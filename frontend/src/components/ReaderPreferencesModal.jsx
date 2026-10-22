import React from 'react';
import { useTheme } from '../themeContext.jsx';
import { X, Type, Sun, Moon, Sparkles, BookOpen, AlignLeft, Check } from 'lucide-react';

export default function ReaderPreferencesModal({ isOpen, onClose }) {
  const { theme, setTheme, readerPrefs, setReaderPrefs } = useTheme();

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4 animate-fade-in"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-2xl border border-subtle bg-surface p-6 shadow-2xl relative text-primary transition-colors"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between pb-4 border-b border-subtle">
          <div className="flex items-center gap-2">
            <BookOpen className="w-5 h-5 text-accent-pri" />
            <h2 className="text-lg font-semibold tracking-wide">Preferências de leitura</h2>
          </div>
          <button
            onClick={onClose}
            aria-label="Fechar preferências"
            className="rounded-lg p-1.5 text-secondary hover:text-primary hover:bg-elevated transition"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="space-y-6 pt-5">
          {/* Theme Selector */}
          <div>
            <label className="text-xs font-semibold uppercase tracking-wider text-muted block mb-2.5">
              Tema
            </label>
            <div className="grid grid-cols-3 gap-2">
              <button
                type="button"
                onClick={() => setTheme('obsidian')}
                className={`flex flex-col items-center justify-center p-3 rounded-xl border text-xs font-medium transition ${
                  theme === 'obsidian'
                    ? 'border-accent-pri bg-elevated shadow-sm text-primary ring-1 ring-accent-pri'
                    : 'border-subtle hover:bg-elevated/50 text-secondary'
                }`}
              >
                <div className="w-4 h-4 rounded-full bg-zinc-900 border border-emerald-500 mb-1.5 flex items-center justify-center">
                  {theme === 'obsidian' && <Check className="w-2.5 h-2.5 text-emerald-400" />}
                </div>
                <span>Obsidian</span>
                <span className="text-[10px] text-muted">Fantasia escura</span>
              </button>

              <button
                type="button"
                onClick={() => setTheme('parchment')}
                className={`flex flex-col items-center justify-center p-3 rounded-xl border text-xs font-medium transition ${
                  theme === 'parchment'
                    ? 'border-accent-pri bg-elevated shadow-sm text-primary ring-1 ring-accent-pri'
                    : 'border-subtle hover:bg-elevated/50 text-secondary'
                }`}
              >
                <div className="w-4 h-4 rounded-full bg-[#fbf6ee] border border-amber-700 mb-1.5 flex items-center justify-center">
                  {theme === 'parchment' && <Check className="w-2.5 h-2.5 text-amber-800" />}
                </div>
                <span>Parchment</span>
                <span className="text-[10px] text-muted">Crônica</span>
              </button>

              <button
                type="button"
                onClick={() => setTheme('oled')}
                className={`flex flex-col items-center justify-center p-3 rounded-xl border text-xs font-medium transition ${
                  theme === 'oled'
                    ? 'border-accent-pri bg-elevated shadow-sm text-primary ring-1 ring-accent-pri'
                    : 'border-subtle hover:bg-elevated/50 text-secondary'
                }`}
              >
                <div className="w-4 h-4 rounded-full bg-black border border-accent-pri mb-1.5 flex items-center justify-center">
                  {theme === 'oled' && <Check className="w-2.5 h-2.5 text-accent-pri" />}
                </div>
                <span>OLED</span>
                <span className="text-[10px] text-muted">Meia-noite</span>
              </button>
            </div>
          </div>

          {/* Font Family */}
          <div>
            <label className="text-xs font-semibold uppercase tracking-wider text-muted block mb-2.5">
              Estilo da fonte
            </label>
            <div className="grid grid-cols-3 gap-2">
              <button
                type="button"
                onClick={() => setReaderPrefs({ fontFamily: 'serif' })}
                className={`p-2.5 rounded-xl border text-xs text-center transition font-serif ${
                  readerPrefs.fontFamily === 'serif'
                    ? 'border-accent-pri bg-elevated text-primary font-semibold ring-1 ring-accent-pri'
                    : 'border-subtle hover:bg-elevated/50 text-secondary'
                }`}
              >
                Serifada (livro)
              </button>
              <button
                type="button"
                onClick={() => setReaderPrefs({ fontFamily: 'sans' })}
                className={`p-2.5 rounded-xl border text-xs text-center transition font-sans ${
                  readerPrefs.fontFamily === 'sans'
                    ? 'border-accent-pri bg-elevated text-primary font-semibold ring-1 ring-accent-pri'
                    : 'border-subtle hover:bg-elevated/50 text-secondary'
                }`}
              >
                Sem serifa
              </button>
              <button
                type="button"
                onClick={() => setReaderPrefs({ fontFamily: 'mono' })}
                className={`p-2.5 rounded-xl border text-xs text-center transition font-mono ${
                  readerPrefs.fontFamily === 'mono'
                    ? 'border-accent-pri bg-elevated text-primary font-semibold ring-1 ring-accent-pri'
                    : 'border-subtle hover:bg-elevated/50 text-secondary'
                }`}
              >
                Monoespaçada
              </button>
            </div>
          </div>

          {/* Font Size */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-xs font-semibold uppercase tracking-wider text-muted">
                Font Size
              </label>
              <span className="text-xs font-mono font-medium text-accent-pri">
                {readerPrefs.fontSize}px
              </span>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-xs text-muted">A</span>
              <input
                type="range"
                min="14"
                max="22"
                step="1"
                value={readerPrefs.fontSize}
                onChange={(e) => setReaderPrefs({ fontSize: Number(e.target.value) })}
                className="w-full accent-emerald-500 h-1.5 bg-elevated rounded-lg appearance-none cursor-pointer"
              />
              <span className="text-base font-bold text-muted">A</span>
            </div>
          </div>

          {/* Line Height & Largura da coluna */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-xs font-semibold uppercase tracking-wider text-muted block mb-2">
                Espaçamento
              </label>
              <div className="flex rounded-lg border border-subtle overflow-hidden p-0.5 bg-elevated/50">
                {[
                  { key: 'normal', label: 'Normal' },
                  { key: 'relaxed', label: 'Folgado' },
                  { key: 'loose', label: 'Largo' },
                ].map((lh) => (
                  <button
                    key={lh.key}
                    type="button"
                    onClick={() => setReaderPrefs({ lineHeight: lh.key })}
                    className={`flex-1 py-1.5 text-[11px] font-medium rounded-md transition ${
                      readerPrefs.lineHeight === lh.key
                        ? 'bg-surface text-primary shadow-sm font-semibold'
                        : 'text-secondary hover:text-primary'
                    }`}
                  >
                    {lh.label}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label className="text-xs font-semibold uppercase tracking-wider text-muted block mb-2">
                Largura da coluna
              </label>
              <div className="flex rounded-lg border border-subtle overflow-hidden p-0.5 bg-elevated/50">
                {[
                  { key: 'compact', label: 'Estreita' },
                  { key: 'standard', label: 'Padrão' },
                  { key: 'wide', label: 'Larga' },
                  { key: 'full', label: 'Cheia' },
                ].map((cw) => (
                  <button
                    key={cw.key}
                    type="button"
                    onClick={() => setReaderPrefs({ columnWidth: cw.key })}
                    className={`flex-1 py-1.5 text-[11px] font-medium rounded-md transition ${
                      readerPrefs.columnWidth === cw.key
                        ? 'bg-surface text-primary shadow-sm font-semibold'
                        : 'text-secondary hover:text-primary'
                    }`}
                  >
                    {cw.label}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="mt-7 pt-4 border-t border-subtle flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 rounded-xl bg-accent-pri hover:bg-accent-hov text-white text-xs font-semibold shadow-md transition"
          >
            Aplicar e fechar
          </button>
        </div>
      </div>
    </div>
  );
}
