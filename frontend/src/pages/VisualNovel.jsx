// Rota do owner: /adventures/:id/novel, dentro do AuthGate.
// Param de produção: `id` ou `adventureId` via useParams. Sem props de mock.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import {
  commandForKey,
  initialCursor,
  isEnd,
  loadVisualNovel,
  positionLabel,
  reduceNovel,
  selectChoice,
  viewIndex,
} from '../api/visualNovel.js'
import './VisualNovel.css'

const KIND_LABEL = {
  do: 'Fazer',
  say: 'Falar',
  story: 'Narrar',
}

export default function VisualNovel() {
  const params = useParams()
  const adventureId = String(params.adventureId || params.id || '').trim()
  const [reloadToken, setReloadToken] = useState(0)
  const [status, setStatus] = useState(adventureId ? 'loading' : 'missing')
  const [error, setError] = useState(null)
  const [model, setModel] = useState(null)
  const [cursor, setCursor] = useState(initialCursor)

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

  const imageIds = useMemo(() => {
    const ids = new Set()
    for (const item of [beats[index], beats[index + 1]]) {
      if (!item) continue
      if (item.background?.id) ids.add(item.background.id)
      for (const person of item.cast || []) {
        if (person.portrait?.id) ids.add(person.portrait.id)
      }
    }
    return [...ids]
  }, [beats, index])
  const images = useNovelBlobs(imageIds, adventureId)

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
        <button type="button" className="vn-btn" onClick={() => setReloadToken((value) => value + 1)}>
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
        <section className="vn-center">
          <h1>{model?.title || 'Aventura sem ações'}</h1>
          <p>Nenhuma ação foi registrada ainda. O replay não inventa cenas.</p>
          <Link className="vn-link" to={`/play/${encodeURIComponent(adventureId)}`}>Voltar ao jogo</Link>
        </section>
      )}

      {status === 'ready' && beat && (
        <section className="vn-stage" aria-label="Cena">
          <StageMedia beat={beat} images={images} />
          <div className="vn-scrim" />
          {beat.cast.length > 0 && <Cast beat={beat} images={images} />}
          <div className="vn-box">
            {ended ? (
              <EndPanel onRestart={() => setCursor(initialCursor())} />
            ) : (
              <BeatBody
                beat={beat}
                rejected={cursor.rejected}
                onChoice={(choice) => setCursor((current) => selectChoice(current, choice, beats))}
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
            <p className="vn-hint">Setas navegam. Home volta ao início. End vai ao fim. Espaço avança fora dos botões.</p>
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
  const src = images.blobFor(ref.id)
  if (src) {
    return <img className="vn-bg" src={src} alt={beat.locationTitle || 'Cena registrada'} />
  }
  const failed = images.failed(ref.id)
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
        const src = person.portrait?.id ? images.blobFor(person.portrait.id) : ''
        const failed = person.portrait?.id ? images.failed(person.portrait.id) : !person.portrait
        return (
          <figure key={person.title} className={person.speaking ? 'vn-person is-speaker' : 'vn-person'}>
            {src ? (
              <img className="vn-portrait" src={src} alt="" />
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

function BeatBody({ beat, rejected, onChoice }) {
  const quote = beat.kind === 'dialogue' && beat.text
  return (
    <div aria-live="polite">
      <p className="vn-speaker">{beat.speakerLabel}</p>
      {beat.text ? (
        <p className="vn-text">{quote ? `“${beat.text}”` : beat.text}</p>
      ) : (
        <p className="vn-empty-line">
          {beat.kind === 'scene' ? 'Ilustração registrada, sem legenda.' : 'Ação registrada, sem texto.'}
        </p>
      )}
      {beat.choices.length > 0 && (
        <div className="vn-choices">
          {beat.choices.map((choice) => (
            <button
              key={`${choice.type}:${choice.actionSeq ?? 'no'}:${choice.text}`}
              type="button"
              className={choice.taken ? 'vn-choice is-taken' : 'vn-choice'}
              data-choice-taken={choice.taken ? 'true' : 'false'}
              onClick={() => onChoice(choice)}
            >
              <span className="vn-choice-kind">
                {KIND_LABEL[choice.type] || choice.type}
                {choice.taken ? ' · caminho registrado' : ' · sugestão não seguida'}
              </span>
              <span className="vn-choice-text">{choice.text || '(sem texto)'}</span>
            </button>
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

function EndPanel({ onRestart }) {
  return (
    <div>
      <p className="vn-speaker">Fim do registro</p>
      <p className="vn-text">Não há ações depois desta. O replay para no que já foi salvo.</p>
      <div className="vn-nav">
        <button type="button" className="vn-btn vn-btn-primary" onClick={onRestart}>
          Rever do início
        </button>
      </div>
    </div>
  )
}

function useNovelBlobs(ids, adventureId) {
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
  }, [adventureId])

  const missingKey = ids.filter(Boolean).slice().sort().join('|')

  useEffect(() => {
    let dead = false
    const wanted = missingKey ? missingKey.split('|') : []
    for (const id of wanted) {
      if (blobs.current[id] || failed.current[id]) continue
      api.fetchImageBlob(id).then((blob) => {
        if (dead) return
        const url = URL.createObjectURL(blob)
        urls.current.push(url)
        blobs.current[id] = url
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
  }, [missingKey, adventureId])

  useEffect(() => () => {
    for (const url of urls.current) URL.revokeObjectURL(url)
  }, [])

  return {
    blobFor(id) {
      void version
      return blobs.current[id] || ''
    },
    failed(id) {
      void version
      return !!failed.current[id]
    },
  }
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
