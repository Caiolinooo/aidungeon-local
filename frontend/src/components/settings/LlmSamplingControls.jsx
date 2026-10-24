import { SettingField } from './Controls.jsx'

export const LLM_DEFAULTS = {
  max_tokens: 300,
  temperature: 0.85,
  top_p: 1,
  min_p: 0.08,
  repetition_penalty: 1.02,
  presence_penalty: 0.15,
  frequency_penalty: 0.1,
  dry_multiplier: 0.8,
  dry_base: 1.75,
  dry_allowed_length: 2,
  dry_penalty_last_n: -1,
  retry_temperature_step: 0.05,
  max_retry_temperature: 1.15,
}

const PRESETS = [
  {
    id: 'faithful',
    label: 'Fiel',
    values: { temperature: 0.45, top_p: 0.9, min_p: 0.05, repetition_penalty: 1.08, presence_penalty: 0, frequency_penalty: 0, dry_multiplier: 1, dry_base: 1.75, dry_allowed_length: 2 },
  },
  {
    id: 'balanced',
    label: 'Equilibrado',
    values: { temperature: 0.85, top_p: 1, min_p: 0.08, repetition_penalty: 1.02, presence_penalty: 0.15, frequency_penalty: 0.1, dry_multiplier: 0.8, dry_base: 1.75, dry_allowed_length: 2 },
  },
  {
    id: 'creative',
    label: 'Criativo',
    values: { temperature: 1.1, top_p: 0.95, min_p: 0.03, repetition_penalty: 1.02, presence_penalty: 0.25, frequency_penalty: 0.15, dry_multiplier: 0.6, dry_base: 1.75, dry_allowed_length: 2 },
  },
]

const LENGTHS = [
  { label: 'Curto', value: 300 },
  { label: 'Médio', value: 600 },
  { label: 'Longo', value: 1200 },
]

export default function LlmSamplingControls({ form, onChange, inputCls, labelCls, compact = false }) {
  const update = (values) => onChange({ ...form, ...values })
  const rangeCls = `w-full accent-accent-600 ${compact ? '' : 'h-1.5 cursor-pointer rounded-lg bg-elevated'}`
  const gridCls = `grid grid-cols-1 gap-4 ${compact ? 'sm:grid-cols-2' : 'sm:grid-cols-3'}`
  const buttonCls = (active) => `rounded-lg px-2.5 py-1 text-xs font-medium transition ${active ? 'bg-accent-pri text-canvas' : 'border border-subtle bg-elevated text-secondary hover:text-primary'}`

  const range = (field, label, min, max, step) => (
    <SettingField label={`${label} (${form[field]})`} labelClassName={labelCls}>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={form[field]}
        onChange={(event) => update({ [field]: Number(event.target.value) })}
        className={rangeCls}
      />
    </SettingField>
  )

  const number = (field, label, props = {}) => (
    <SettingField label={label} labelClassName={labelCls}>
      <input
        type="number"
        value={form[field]}
        onChange={(event) => update({ [field]: event.target.value })}
        className={inputCls}
        {...props}
      />
    </SettingField>
  )

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <span className={labelCls}>Estilo da narrativa</span>
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Predefinições de amostragem">
            {PRESETS.map((preset) => {
              const active = Object.entries(preset.values).every(([key, value]) => Number(form[key]) === value)
              return (
                <button key={preset.id} type="button" aria-pressed={active} onClick={() => update(preset.values)} className={buttonCls(active)}>
                  {preset.label}
                </button>
              )
            })}
          </div>
        </div>
        <button type="button" onClick={() => update(LLM_DEFAULTS)} className="rounded-lg border border-subtle px-2.5 py-1 text-xs text-secondary hover:text-primary">
          Restaurar padrões
        </button>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <SettingField label={`Tamanho da resposta (${form.max_tokens} tokens)`} labelClassName={labelCls}>
          <input
            type="range"
            min={100}
            max={2048}
            step={1}
            value={form.max_tokens}
            onChange={(event) => update({ max_tokens: Number(event.target.value) })}
            className={rangeCls}
          />
        </SettingField>
        <div>
          <span className={labelCls}>Tamanho predefinido</span>
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Tamanho da resposta">
            {LENGTHS.map((length) => (
              <button key={length.value} type="button" aria-pressed={Number(form.max_tokens) === length.value} onClick={() => update({ max_tokens: length.value })} className={buttonCls(Number(form.max_tokens) === length.value)}>
                {length.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className={gridCls}>
        {range('temperature', 'Temperatura', 0, 2, 0.05)}
        {range('top_p', 'Top P', 0, 1, 0.05)}
        {range('min_p', 'Min P', 0, 1, 0.01)}
      </div>
      <div className={gridCls}>
        {range('presence_penalty', 'Penalidade de presença', -2, 2, 0.05)}
        {range('frequency_penalty', 'Penalidade de frequência', -2, 2, 0.05)}
        {range('repetition_penalty', 'Penalidade de repetição', 1, 2, 0.01)}
      </div>

      <fieldset className="space-y-4 rounded-xl border border-subtle bg-elevated/30 p-3">
        <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-muted">Avançado: DRY e novas tentativas</legend>
        <div className={gridCls}>
          {number('dry_multiplier', 'Multiplicador DRY', { min: 0, max: 5, step: 0.1 })}
          {number('dry_base', 'Base DRY', { min: 1, max: 3, step: 0.05 })}
          {number('dry_allowed_length', 'Comprimento permitido DRY', { min: 1, max: 10, step: 1 })}
        </div>
        <div className={gridCls}>
          {number('dry_penalty_last_n', 'Tokens considerados pelo DRY (-1 = todos)', { min: -1, step: 1 })}
          {number('retry_temperature_step', 'Incremento de temperatura na repetição', { min: 0, max: 0.5, step: 0.01 })}
          {number('max_retry_temperature', 'Temperatura máxima na repetição', { min: 0, max: 2, step: 0.05 })}
        </div>
      </fieldset>
    </div>
  )
}
