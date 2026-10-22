/** @type {import('tailwindcss').Config} */
import colors from 'tailwindcss/colors'

const STEPS = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]
// Ramp vars are `--n-950` (the triplet), not `--n-950-rgb`. The `-rgb` suffix
// belongs only to the semantic tokens (`--bg-canvas-rgb` and friends).
const ramp = (prefix) =>
  Object.fromEntries(STEPS.map((s) => [s, `rgb(var(--${prefix}-${s}) / <alpha-value>)`]))

const drop = new Set(['lightBlue', 'warmGray', 'trueGray', 'coolGray', 'blueGray'])
const safeColors = Object.fromEntries(
  Object.keys(colors)
    .filter((name) => !drop.has(name))
    .map((name) => [name, colors[name]]),
)
const base = { ...safeColors, transparent: 'transparent', current: 'currentColor', inherit: 'inherit' }

export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    colors: {
      ...base,
      zinc: ramp('n'),
      emerald: ramp('accent'),
      red: ramp('danger'),
      amber: ramp('warn'),
      // Aliases so the off-ramp replacements (danger/warn) resolve to the same triplets.
      danger: ramp('danger'),
      warn: ramp('warn'),
    },
    extend: {
      colors: {
        canvas: 'rgb(var(--bg-canvas-rgb) / <alpha-value>)',
        surface: 'rgb(var(--bg-surface-rgb) / <alpha-value>)',
        elevated: 'rgb(var(--bg-elevated-rgb) / <alpha-value>)',
        'surface-input': 'rgb(var(--bg-input-rgb) / <alpha-value>)',
        subtle: 'rgb(var(--border-subtle-rgb) / <alpha-value>)',
        'accent-pri': 'rgb(var(--accent-primary-rgb) / <alpha-value>)',
        'accent-hov': 'rgb(var(--accent-hover-rgb) / <alpha-value>)',
        'accent-gld': 'rgb(var(--accent-gold-rgb) / <alpha-value>)',
        info: 'rgb(var(--info-text-rgb) / <alpha-value>)',
      },
      textColor: {
        primary: 'rgb(var(--text-primary-rgb) / <alpha-value>)',
        secondary: 'rgb(var(--text-secondary-rgb) / <alpha-value>)',
        muted: 'rgb(var(--text-muted-rgb) / <alpha-value>)',
      },
      fontFamily: {
        prose: ['Georgia', 'Cambria', 'Times New Roman', 'serif'],
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'Menlo', 'monospace'],
      },
      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        'slide-in-top': { from: { opacity: '0', transform: 'translateY(-8px)' }, to: { opacity: '1', transform: 'none' } },
      },
      animation: {
        'fade-in': 'fade-in 200ms ease-out',
        'slide-in-top': 'slide-in-top 220ms ease-out',
      },
    },
  },
  plugins: [],
}
