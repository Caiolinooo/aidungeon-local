const DEFAULT_CHIP = 'rounded-md border border-subtle bg-elevated px-2 py-1 text-[10px] font-medium text-secondary hover:text-primary transition disabled:opacity-40'

export default function ClipActions({
  pending = false,
  canStart = true,
  hasClip = false,
  showingVideo = false,
  downloadHref = '',
  downloadName = 'clipe.mp4',
  onStart,
  onCancel,
  onShowImage,
  onShowClip,
  chipClass = DEFAULT_CHIP,
  className = '',
}) {
  return (
    <div className={`flex flex-wrap items-center gap-1.5 ${className}`}>
      {pending ? (
        <>
          <span className="text-[10px] font-medium text-muted" role="status">Animando…</span>
          <button type="button" onClick={onCancel} className={chipClass}>
            Cancelar
          </button>
        </>
      ) : (
        <button type="button" onClick={onStart} disabled={!canStart} className={chipClass}>
          Animar 5 s
        </button>
      )}
      {hasClip && showingVideo && (
        <button type="button" onClick={onShowImage} className={chipClass}>
          Voltar à imagem
        </button>
      )}
      {hasClip && !showingVideo && !pending && (
        <button type="button" onClick={onShowClip} className={chipClass}>
          Ver clipe
        </button>
      )}
      {hasClip && downloadHref && (
        <a href={downloadHref} download={downloadName} className={chipClass}>
          Baixar MP4
        </a>
      )}
    </div>
  )
}
