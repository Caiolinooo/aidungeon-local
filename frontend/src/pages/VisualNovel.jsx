// Rota do owner: /adventures/:id/novel, dentro do AuthGate.
// Param de produção: `id` ou `adventureId` via useParams. Sem props de mock.
// Replay navega o registro. A resposta própria só sai no beat mais recente,
// pelo POST /action (stream). Escolhas do replay não enviam ação nova.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import {
  beginReply,
  canWriteReply,
  commandForKey,
  imageMediaKind,
  initialCursor,
  isEnd,
  latestTurnBeats,
  loadVisualNovel,
  novelAfterSubmit,
  positionLabel,
  presentChoices,
  reduceNovel,
  replyKeyAction,
  selectChoice,
  submitNovelAction,
  viewIndex,
} from '../api/visualNovel.js'
import './VisualNovel.css'

const KIND_LABEL = {
  do: 'Fazer',
  say: 'Falar',
  story: 'Narrar',
}

const REPLY_TYPES = [
  { id: 'do', label: 'Fazer', placeholder: 'O que você faz?' },
  { id: 'say', label: 'Falar', placeholder: 'O que você diz?' },
  { id: 'story', label: 'Narrar', placeholder: 'O que acontece em seguida?' },
]

export default function VisualNovel() {
  const params = useParams()
  const adventureId = String(params.adventureId || params.id || '').trim()
  const [reloadToken, setReloadToken] = useState(0)
  const [status, setStatus] = useState(adventureId ? 'loading' : 'missing')
  const [error, setError] = useState(null)
  const [model, setModel] = useState(null)
  const [cursor, setCursor] = useState(initialCursor)
  const [replyType, setReplyType] = useState('do')
  const [draft, setDraft] = useState('')
  const [phase, setPhase] = useState('idle')
  const [streamText, setStreamText] = useState('')
  const [submitError, setSubmitError] = useState(null)
  const [notice, setNotice] = useState('')
  const modelRef = useRef(null)
  const busyRef = useRef(false)
  const abortRef = useRef(null)
  const submitGen = useRef(0)
  modelRef.current = model
  const busy = phase !== 'idle'

  useEffect(() => {
    setDraft('')
    setReplyType('do')
    setSubmitError(null)
    setNotice('')
    setStreamText('')
    setPhase('idle')
    busyRef.current = false
    return () => {
      submitGen.current += 1
      abortRef.current?.abort()
      abortRef.current = null
      busyRef.current = false
    }
  }, [adventureId])

  useEffect(() => {
    if (!adventureId) return undefined
    const controller = new AbortController()
    setStatus('loading')
    setError(null)
    setModel(null)
    setCursor(initialCursor())
    loadVisualNovel(adventureId, { signal: controller.signal })
      .then((next) => {
        setModel(next)
        setCursor(initialCursor())
        setStatus('ready')
      })
      .catch((err) => {
        if (err?.name === 'AbortError') return
        setError(err)
        setStatus('error')
      })
    return () => controller.abort()
  }, [adventureId, reloadToken])

  const beats = model?.beats || []
  const ended = isEnd(cursor, beats.length)
  const index = viewIndex(cursor, beats.length)
  const beat = index >= 0 ? beats[index] : null
  const writable = status === 'ready' && canWriteReply(cursor, beats.length)
  const showReply = writable || busy
  const shownBeats = latestTurnBeats(beats, writable ? index : -1)
  const turnBeats = shownBeats.length > 0 ? shownBeats : (beat ? [beat] : [])
  const stageBeat = beat && turnBeats.length > 1
    ? { ...beat, cast: mergeCast(turnBeats) }
    : beat

  const imageIds = useMemo(() => {
    const ids = new Set()
    const visible = []
    if (stageBeat) visible.push(stageBeat)
    if (beats[index + 1]) visible.push(beats[index + 1])
    for (const item of visible) {
      if (item.background?.id) ids.add(item.background.id)
      for (const person of item.cast || []) {
        if (person.portrait?.id) ids.add(person.portrait.id)
      }
    }
    return [...ids]
  }, [beats, stageBeat, index])
  const images = useNovelBlobs(imageIds, adventureId, reloadToken)

  useEffect(() => {
    const onKey = (event) => {
      if (status !== 'ready' || beats.length === 0) return
      const command = commandForKey(event.key, {
        tag: event.target?.tagName,
        editable: event.target?.isContentEditable,
        altKey: event.altKey,
        metaKey: event.metaKey,
        ctrlKey: event.ctrlKey,
      })
      if (!command) return
      event.preventDefault()
      setCursor((current) => reduceNovel(current, command, beats.length))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [status, beats.length])

  const sendReply = async () => {
    const begun = beginReply({ busy: busyRef.current, text: draft, type: replyType })
    if (!begun || !adventureId) return
    const generation = submitGen.current + 1
    submitGen.current = generation
    busyRef.current = true
    setPhase('sending')
    setSubmitError(null)
    setNotice('')
    setStreamText('')
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const result = await submitNovelAction(adventureId, {
        type: begun.type,
        text: begun.text,
        signal: controller.signal,
        onToken: (token) => {
          if (submitGen.current !== generation) return
          setPhase('streaming')
          setStreamText((prev) => prev + String(token ?? ''))
        },
      })
      if (submitGen.current !== generation) return
      const applied = novelAfterSubmit(modelRef.current, result)
      if (applied.intent) {
        setDraft('')
        setStreamText('')
        setNotice('Intenção enviada ao mestre. Nenhuma narração foi gerada.')
        return
      }
      if (!applied.committed) {
        setStreamText('')
        setSubmitError('O stream terminou sem gravar a resposta.')
        return
      }
      setModel(applied.model)
      setCursor({ index: Math.max(0, applied.model.beats.length - 1), rejected: '' })
      setDraft('')
      setStreamText('')
    } catch (err) {
      if (submitGen.current !== generation || err?.name === 'AbortError') return
      setStreamText('')
      setSubmitError(errorText(err))
    } finally {
      if (submitGen.current === generation) {
        busyRef.current = false
        setPhase('idle')
        if (abortRef.current === controller) abortRef.current = null
      }
    }
  }

  if (!adventureId) {
    return (
      <main className="vn-root">
        <section className="vn-center">
          <h1>Aventura não informada</h1>
          <p>A rota precisa trazer o id da aventura.</p>
          <Link className="vn-link" to="/">Voltar</Link>
        </section>
      </main>
    )
  }

  return (
    <main className="vn-root">
      <header className="vn-bar">
        <Link className="vn-link" to={`/play/${encodeURIComponent(adventureId)}`}>Jogo</Link>
        <h1 className="vn-title">{model?.title || 'Visual novel'}</h1>
        <p className="vn-position" aria-live="polite">
          {status === 'ready' ? positionLabel(cursor.index, beats.length) : '…'}
        </p>
        <button
          type="button"
          className="vn-btn"
          disabled={busy}
          onClick={() => setReloadToken((value) => value + 1)}
        >
          Recarregar
        </button>
      </header>

      {status === 'loading' && (
        <section className="vn-center" role="status">
          <h1>Carregando o registro</h1>
          <p>Ações, retratos e cenários já salvos nesta aventura.</p>
        </section>
      )}

      {status === 'error' && (
        <section className="vn-center" role="alert">
          <h1>Não foi possível abrir o replay</h1>
          <p>{errorText(error)}</p>
          <div className="vn-actions">
            <button type="button" className="vn-btn vn-btn-primary" onClick={() => setReloadToken((value) => value + 1)}>
              Tentar de novo
            </button>
            <Link className="vn-link" to={`/play/${encodeURIComponent(adventureId)}`}>Voltar ao jogo</Link>
          </div>
        </section>
      )}

      {status === 'ready' && beats.length === 0 && (
        <section className="vn-center vn-center-start">
          <h1>{model?.title || 'Aventura sem ações'}</h1>
          <p>Nenhuma ação foi registrada ainda. O replay não inventa cenas. A primeira resposta abaixo é enviada de verdade.</p>
          <ReplyForm
            type={replyType}
            draft={draft}
            phase={phase}
            streamText={streamText}
            error={submitError}
            notice={notice}
            onType={setReplyType}
            onDraft={setDraft}
            onSubmit={sendReply}
          />
        </section>
      )}

      {status === 'ready' && beat && (
        <section className="vn-stage" aria-label="Cena">
          <StageMedia beat={stageBeat} images={images} />
          <div className="vn-scrim" />
          <div className="vn-foreground">
            {stageBeat.cast.length > 0 && <Cast beat={stageBeat} images={images} />}
            <div className="vn-box">
              {ended ? (
                <EndPanel onRestart={() => setCursor(initialCursor())} />
              ) : (
                <div aria-live="polite">
                  {turnBeats.map((item, itemIndex) => (
                    <BeatBody
                      key={item.key}
                      beat={item}
                      rejected={itemIndex === turnBeats.length - 1 ? cursor.rejected : ''}
                      showChoices={itemIndex === turnBeats.length - 1}
                      onChoice={(choice) => setCursor((current) => selectChoice(current, choice, beats))}
                    />
                  ))}
                </div>
              )}
              {!writable && !busy && (
                <p className="vn-note">Para continuar a história, volte ao último beat e escreva a resposta. O meio do replay não envia ação.</p>
              )}
              {showReply && (
                <ReplyForm
                  type={replyType}
                  draft={draft}
                  phase={phase}
                  streamText={streamText}
                  error={submitError}
                  notice={notice}
                  onType={setReplyType}
                  onDraft={setDraft}
                  onSubmit={sendReply}
                />
              )}
              <nav className="vn-nav" aria-label="Replay">
                <button
                  type="button"
                  className="vn-btn"
                  disabled={cursor.index <= 0}
                  onClick={() => setCursor((current) => reduceNovel(current, { type: 'prev' }, beats.length))}
                >
                  Anterior
                </button>
                <button
                  type="button"
                  className="vn-btn vn-btn-primary"
                  disabled={ended}
                  onClick={() => setCursor((current) => reduceNovel(current, { type: 'next' }, beats.length))}
                >
                  Próximo
                </button>
                <button
                  type="button"
                  className="vn-btn"
                  onClick={() => setCursor(initialCursor())}
                >
                  Do início
                </button>
              </nav>
              <p className="vn-hint">Setas, Home e End navegam o replay. No último beat, Enter ou Ctrl+Enter envia. Shift+Enter quebra a linha.</p>
            </div>
          </div>
        </section>
      )}
    </main>
  )
}

function StageMedia({ beat, images }) {
  const ref = beat.background
  if (!ref?.id) {
    return (
      <div className="vn-plate" role="img" aria-label={beat.locationTitle || 'Sem cenário registrado'}>
        <p className="vn-plate-kicker">{beat.locationTitle ? 'Lugar registrado' : 'Sem cenário'}</p>
        <p className="vn-plate-title">{beat.locationTitle || 'Nenhuma imagem registrada'}</p>
      </div>
    )
  }
  const media = images.mediaFor(ref.id)
  const failed = images.failed(ref.id)
  if (media?.url && media.kind === 'video') {
    return (
      <video
        className="vn-bg"
        src={media.url}
        muted
        playsInline
        autoPlay
        loop
        aria-label={beat.locationTitle || 'Cena registrada'}
        onError={() => images.markFailed(ref.id)}
      />
    )
  }
  if (media?.url) {
    return (
      <img
        className="vn-bg"
        src={media.url}
        alt={beat.locationTitle || 'Cena registrada'}
        onError={() => images.markFailed(ref.id)}
      />
    )
  }
  return (
    <div className="vn-plate" role="img" aria-label={failed ? 'Imagem registrada indisponível' : 'Abrindo imagem registrada'}>
      <p className="vn-plate-kicker">{failed ? 'Imagem indisponível' : 'Imagem registrada'}</p>
      <p className="vn-plate-title">{beat.locationTitle || (failed ? 'Não foi possível abrir' : 'Abrindo…')}</p>
    </div>
  )
}

function Cast({ beat, images }) {
  return (
    <div className="vn-cast">
      {beat.cast.map((person) => {
        const id = person.portrait?.id || ''
        const media = id ? images.mediaFor(id) : null
        const failed = id ? images.failed(id) : !person.portrait
        return (
          <figure key={person.title} className={person.speaking ? 'vn-person is-speaker' : 'vn-person'}>
            {media?.url && media.kind === 'video' ? (
              <video
                className="vn-portrait"
                src={media.url}
                muted
                playsInline
                autoPlay
                loop
                onError={() => images.markFailed(id)}
              />
            ) : media?.url ? (
              <img className="vn-portrait" src={media.url} alt="" onError={() => images.markFailed(id)} />
            ) : (
              <div className="vn-fallback" aria-label={failed ? `${person.title}, sem retrato` : person.title}>
                {initials(person.title)}
              </div>
            )}
            <figcaption>{person.title}</figcaption>
          </figure>
        )
      })}
    </div>
  )
}

function BeatBody({ beat, rejected, onChoice, showChoices = true }) {
  const quote = beat.kind === 'dialogue' && beat.text
  const navigation = showChoices ? presentChoices(beat.choices) : { navigationOnly: true, items: [] }
  return (
    <div className="vn-turn">
      <p className="vn-speaker">{beat.speakerLabel}</p>
      {beat.text ? (
        <p className="vn-text">{quote ? `“${beat.text}”` : beat.text}</p>
      ) : (
        <p className="vn-empty-line">
          {beat.kind === 'scene' ? 'Ilustração registrada, sem legenda.' : 'Ação registrada, sem texto.'}
        </p>
      )}
      {navigation.items.length > 0 && (
        <div className="vn-choices">
          <p className="vn-choice-legend">Navegação do replay. Estes itens não enviam uma ação nova.</p>
          {navigation.items.map((choice) => (
            choice.effect === 'jump' ? (
              <button
                key={`${choice.type}:${choice.actionSeq}:${choice.text}`}
                type="button"
                className="vn-choice is-taken"
                data-choice-taken="true"
                onClick={() => onChoice(choice)}
              >
                <span className="vn-choice-kind">
                  {KIND_LABEL[choice.type] || choice.type}
                  {' · ir para o trecho'}
                </span>
                <span className="vn-choice-text">{choice.text || '(sem texto)'}</span>
              </button>
            ) : (
              <p
                key={`${choice.type}:no:${choice.text}`}
                className="vn-record"
                data-choice-taken="false"
              >
                <span className="vn-choice-kind">
                  {KIND_LABEL[choice.type] || choice.type}
                  {' · sugestão não seguida'}
                </span>
                <span className="vn-choice-text">{choice.text || '(sem texto)'}</span>
              </p>
            )
          ))}
        </div>
      )}
      {rejected ? (
        <p className="vn-note" role="status">
          “{rejected}” não é o caminho registrado. O replay não cria outro ramo.
        </p>
      ) : null}
    </div>
  )
}

function ReplyForm({
  type, draft, phase, streamText, error, notice, onType, onDraft, onSubmit,
}) {
  const busy = phase !== 'idle'
  const active = REPLY_TYPES.find((item) => item.id === type) || REPLY_TYPES[0]
  const onKeyDown = (event) => {
    const action = replyKeyAction({
      key: event.key,
      shiftKey: event.shiftKey,
      ctrlKey: event.ctrlKey,
      metaKey: event.metaKey,
      isComposing: event.nativeEvent?.isComposing,
      keyCode: event.nativeEvent?.keyCode,
    })
    if (action !== 'send') return
    event.preventDefault()
    onSubmit()
  }
  return (
    <form
      className="vn-reply"
      aria-busy={busy}
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit()
      }}
    >
      <div className="vn-reply-types" role="radiogroup" aria-label="Tipo da resposta">
        {REPLY_TYPES.map((item) => (
          <button
            key={item.id}
            type="button"
            role="radio"
            aria-checked={type === item.id}
            className={type === item.id ? 'vn-btn vn-btn-primary' : 'vn-btn'}
            disabled={busy}
            onClick={() => onType(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>
      <label className="vn-reply-label" htmlFor="vn-reply-text">Sua resposta</label>
      {phase === 'sending' && <p className="vn-note" role="status">Enviando…</p>}
      {phase === 'streaming' && (
        <div className="vn-stream" role="status" aria-live="polite">
          <p className="vn-speaker">Narração ao vivo</p>
          <p className="vn-text">{streamText || 'Aguardando o texto…'}</p>
        </div>
      )}
      <textarea
        id="vn-reply-text"
        className="vn-reply-text"
        value={draft}
        placeholder={active.placeholder}
        rows={3}
        disabled={busy}
        aria-invalid={error ? 'true' : 'false'}
        aria-describedby="vn-reply-hint vn-reply-status"
        onChange={(event) => onDraft(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="vn-reply-row">
        <button type="submit" className="vn-btn vn-btn-primary" disabled={busy || !draft.trim()}>
          {phase === 'sending' ? 'Enviando…' : phase === 'streaming' ? 'Narrando…' : 'Enviar'}
        </button>
        <p id="vn-reply-hint" className="vn-hint">Enter ou Ctrl+Enter envia. Shift+Enter quebra a linha.</p>
      </div>
      <p
        id="vn-reply-status"
        className={error ? 'vn-error' : 'vn-note'}
        role={error ? 'alert' : 'status'}
        hidden={!error && !notice}
      >
        {error || notice || ''}
      </p>
    </form>
  )
}

function EndPanel({ onRestart }) {
  return (
    <div>
      <p className="vn-speaker">Fim do registro</p>
      <p className="vn-text">Não há ações depois desta. Volte ao último beat para escrever a próxima resposta.</p>
      <div className="vn-nav">
        <button type="button" className="vn-btn vn-btn-primary" onClick={onRestart}>
          Rever do início
        </button>
      </div>
    </div>
  )
}

function useNovelBlobs(ids, adventureId, epoch) {
  const [version, setVersion] = useState(0)
  const blobs = useRef({})
  const failed = useRef({})
  const urls = useRef([])

  useEffect(() => {
    blobs.current = {}
    failed.current = {}
    for (const url of urls.current) URL.revokeObjectURL(url)
    urls.current = []
    setVersion((value) => value + 1)
  }, [adventureId, epoch])

  const missingKey = ids.filter(Boolean).slice().sort().join('\n')

  useEffect(() => {
    let dead = false
    const wanted = missingKey ? missingKey.split('\n') : []
    for (const id of wanted) {
      if (blobs.current[id] || failed.current[id]) continue
      api.fetchImageBlob(id).then((blob) => {
        if (dead) return
        const kind = imageMediaKind(blob?.type)
        if (!blob || blob.size === 0 || kind === 'invalid') {
          failed.current[id] = true
          setVersion((value) => value + 1)
          return
        }
        const url = URL.createObjectURL(blob)
        if (dead) {
          URL.revokeObjectURL(url)
          return
        }
        urls.current.push(url)
        blobs.current[id] = { url, kind }
        setVersion((value) => value + 1)
      }).catch((error) => {
        if (dead || error?.name === 'AbortError') return
        failed.current[id] = true
        setVersion((value) => value + 1)
      })
    }
    return () => {
      dead = true
    }
  }, [missingKey, adventureId, epoch])

  useEffect(() => () => {
    for (const url of urls.current) URL.revokeObjectURL(url)
  }, [])

  return {
    mediaFor(id) {
      void version
      return blobs.current[id] || null
    },
    failed(id) {
      void version
      return !!failed.current[id]
    },
    markFailed(id) {
      if (!id || failed.current[id]) return
      const current = blobs.current[id]
      if (current?.url) {
        URL.revokeObjectURL(current.url)
        urls.current = urls.current.filter((url) => url !== current.url)
      }
      delete blobs.current[id]
      failed.current[id] = true
      setVersion((value) => value + 1)
    },
  }
}

function mergeCast(turnBeats) {
  const cast = []
  const seen = new Set()
  for (const item of turnBeats) {
    for (const person of item.cast || []) {
      const key = String(person?.title || '').trim().toLowerCase()
      if (!key || seen.has(key)) continue
      seen.add(key)
      cast.push(person)
    }
  }
  return cast
}

function initials(title) {
  const words = String(title || '').trim().split(/\s+/).filter(Boolean)
  if (words.length === 0) return '·'
  return words.slice(0, 2).map((word) => word[0].toUpperCase()).join('')
}

function errorText(error) {
  if (error?.status === 404) return 'Aventura não encontrada.'
  if (error?.status === 0) return 'Sem conexão com o servidor.'
  return error?.message || 'Falha ao buscar a aventura.'
}
