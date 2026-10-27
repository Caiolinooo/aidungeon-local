import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../api.js'
import {
  deletePublication,
  getPublication,
  getPublicationVersion,
  likePublication,
  listPublications,
  publishPublication,
  remixPublication,
  unlikePublication,
} from '../api/publications.js'
import PublicationPlayModal from '../components/PublicationPlayModal.jsx'
import { useToast } from '../components/Toast.jsx'

const inputCls =
  'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600'

function coverSrc(value) {
  const text = String(value || '')
  if (/^https?:\/\//i.test(text) || text.startsWith('/')) return text
  return ''
}

function kindLabel(kind) {
  return kind === 'adventure' ? 'Aventura' : 'Scenario'
}

export default function Catalog({ onPlay, onRemix }) {
  const navigate = useNavigate()
  const [playTarget, setPlayTarget] = useState(null)
  const toast = useToast()
  const notify = (message) => {
    if (typeof toast === 'function') toast(message)
  }
  const [me, setMe] = useState(null)
  const [draftQ, setDraftQ] = useState('')
  const [draftTag, setDraftTag] = useState('')
  const [q, setQ] = useState('')
  const [tag, setTag] = useState('')
  const [sort, setSort] = useState('new')
  const [items, setItems] = useState(null)
  const [detail, setDetail] = useState(null)
  const [busy, setBusy] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [remixNote, setRemixNote] = useState(null)
  const [publishForm, setPublishForm] = useState({
    kind: 'scenario',
    source_id: '',
    cover_image: '',
  })

  const load = useCallback(async () => {
    const list = await listPublications({ q, tag, sort })
    setItems(list)
    return list
  }, [q, tag, sort])

  useEffect(() => {
    let cancel = false
    api.checkAuth()
      .then((session) => {
        if (!cancel) setMe(session)
      })
      .catch(() => {
        if (!cancel) setMe(null)
      })
    return () => {
      cancel = true
    }
  }, [])

  useEffect(() => {
    let cancel = false
    load()
      .catch((error) => {
        if (cancel) return
        notify(error.message)
        setItems([])
      })
    return () => {
      cancel = true
    }
  }, [load]) // eslint-disable-line react-hooks/exhaustive-deps

  async function openPublication(id, version) {
    setBusy(true)
    setRemixNote(null)
    try {
      const next = version == null
        ? await getPublication(id)
        : await getPublicationVersion(id, version)
      setDetail(next)
    } catch (error) {
      notify(error.message)
    } finally {
      setBusy(false)
    }
  }

  function applySearch(event) {
    event.preventDefault()
    setQ(draftQ.trim())
    setTag(draftTag.trim())
  }

  function applyTag(next) {
    setDraftTag(next)
    setTag(next)
  }

  async function toggleLike(pub) {
    setBusy(true)
    try {
      const result = pub.liked
        ? await unlikePublication(pub.id)
        : await likePublication(pub.id)
      setItems((list) => (list || []).map((item) => (
        item.id === pub.id
          ? { ...item, liked: result.liked, like_count: result.likes }
          : item
      )))
      setDetail((current) => (
        current && current.id === pub.id
          ? { ...current, liked: result.liked, like_count: result.likes }
          : current
      ))
    } catch (error) {
      notify(error.message)
    } finally {
      setBusy(false)
    }
  }

  async function remix(pub) {
    const version = pub.selected_version || pub.latest_version
    setBusy(true)
    try {
      const created = await remixPublication(pub.id, version)
      const payload = {
        publicationId: pub.id,
        version: created.parent_version,
        scenarioId: created.scenario_id,
        title: created.title,
        scenario: created.scenario,
      }
      if (typeof onRemix === 'function') onRemix(payload)
      else navigate(`/scenarios/${created.scenario_id}/edit`)
      notify(`Remix v${created.parent_version}: ${created.title}`)
    } catch (error) {
      notify(error.message)
    } finally {
      setBusy(false)
    }
  }

  function play(pub) {
    if (typeof onPlay === 'function') {
      onPlay({ publicationId: pub.id, version: pub.selected_version, title: pub.title })
    } else {
      setPlayTarget(pub)
    }
  }

  async function remove(pub) {
    setBusy(true)
    try {
      await deletePublication(pub.id)
      setDetail(null)
      setRemoving(false)
      setRemixNote(null)
      await load()
    } catch (error) {
      notify(error.message)
    } finally {
      setBusy(false)
    }
  }

  async function publish(event) {
    event.preventDefault()
    setBusy(true)
    try {
      const created = await publishPublication(publishForm)
      setPublishForm({ kind: 'scenario', source_id: '', cover_image: '' })
      await load()
      await openPublication(created.id, created.selected_version)
      notify(created.created ? 'Publicado' : 'Essa versão já estava publicada')
    } catch (error) {
      notify(error.message)
    } finally {
      setBusy(false)
    }
  }

  const canDelete = Boolean(
    detail && me && (me.admin || me.user === detail.owner),
  )
  const snapshot = detail?.snapshot
  const activeHooks = snapshot
    ? Object.entries(snapshot.scripts || {}).filter(([, text]) => String(text || '').trim())
    : []

  return (
    <div className="mx-auto flex h-full max-w-6xl flex-col gap-4 overflow-y-auto px-4 py-8 text-zinc-100">
      <div>
        <Link to="/" className="text-sm text-emerald-400 hover:underline">← Aventuras</Link>
        <h1 className="mt-2 text-2xl font-bold">Catálogo</h1>
        <p className="text-sm text-zinc-500">
          Cenários e aventuras publicados. Cada versão fica congelada.
        </p>
      </div>

      <form onSubmit={applySearch} className="grid gap-2 rounded-xl border border-zinc-800 bg-zinc-900 p-4 sm:grid-cols-[1fr_10rem_9rem_auto]">
        <input
          value={draftQ}
          onChange={(event) => setDraftQ(event.target.value)}
          placeholder="Buscar título ou descrição"
          aria-label="Buscar"
          data-testid="catalog-search"
          className={inputCls}
        />
        <input
          value={draftTag}
          onChange={(event) => setDraftTag(event.target.value)}
          placeholder="Tag"
          aria-label="Tag"
          data-testid="catalog-tag"
          className={inputCls}
        />
        <select
          value={sort}
          onChange={(event) => setSort(event.target.value)}
          aria-label="Ordenação"
          data-testid="catalog-sort"
          className={inputCls}
        >
          <option value="new">Novas</option>
          <option value="likes">Curtidas</option>
        </select>
        <button
          type="submit"
          className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600"
        >
          Filtrar
        </button>
      </form>

      <div className="grid gap-4 lg:grid-cols-[1fr_1.15fr]">
        <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">Publicações</h2>
          {items === null ? (
            <p className="text-sm text-zinc-500">Carregando…</p>
          ) : items.length === 0 ? (
            <p className="text-sm text-zinc-500">Nenhuma publicação encontrada.</p>
          ) : (
            <ul className="flex flex-col gap-3">
              {items.map((item) => {
                const src = coverSrc(item.cover_image)
                const selected = detail?.id === item.id
                return (
                  <li key={item.id}>
                    <div
                      role="button"
                      tabIndex={0}
                      onClick={() => openPublication(item.id)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') openPublication(item.id)
                      }}
                      className={`flex w-full cursor-pointer gap-3 rounded-lg border p-3 text-left hover:border-zinc-600 ${
                        selected ? 'border-emerald-700 bg-zinc-950' : 'border-zinc-800'
                      }`}
                    >
                      {src ? (
                        <img src={src} alt="" className="h-16 w-24 shrink-0 rounded-md object-cover" />
                      ) : (
                        <div className="flex h-16 w-24 shrink-0 items-center justify-center rounded-md bg-zinc-800 text-lg font-semibold text-zinc-400">
                          {(item.title || '?').slice(0, 1)}
                        </div>
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="truncate font-medium">{item.title}</div>
                        <p className="line-clamp-2 text-sm text-zinc-400">{item.description || 'Sem descrição'}</p>
                        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
                          <span>{kindLabel(item.kind)}</span>
                          <span>v{item.latest_version}</span>
                          <span>{item.like_count} curtidas</span>
                        </div>
                        <div className="mt-1 flex flex-wrap gap-1">
                          {(item.tags || []).map((itemTag) => (
                            <span
                              key={itemTag}
                              role="link"
                              tabIndex={0}
                              onClick={(event) => {
                                event.stopPropagation()
                                applyTag(itemTag)
                              }}
                              onKeyDown={(event) => {
                                if (event.key === 'Enter') {
                                  event.stopPropagation()
                                  applyTag(itemTag)
                                }
                              }}
                              className="rounded-full border border-zinc-700 px-2 py-0.5 text-xs text-zinc-300"
                            >
                              {itemTag}
                            </span>
                          ))}
                        </div>
                      </div>
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </section>

        <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
          {!detail || !snapshot ? (
            <p className="text-sm text-zinc-500">Escolha uma publicação para ver versões, remix e jogar.</p>
          ) : (
            <div className="flex flex-col gap-3">
              <div>
                <h2 className="text-xl font-semibold">{detail.title}</h2>
                <p className="text-sm text-zinc-500">
                  {kindLabel(detail.kind)} · {detail.owner} · v{detail.selected_version} de {detail.latest_version}
                </p>
                <p className="mt-2 text-sm text-zinc-300">{detail.description}</p>
              </div>
              {snapshot.opening_text ? (
                <p className="line-clamp-6 whitespace-pre-wrap text-sm text-zinc-400">{snapshot.opening_text}</p>
              ) : null}
              <p className="text-xs text-zinc-500">
                {(snapshot.links || []).length} links
                {activeHooks.length ? ` · scripts: ${activeHooks.map(([hook]) => hook).join(', ')}` : ''}
                {' · '}
                <span className="font-mono">{(detail.content_hash || '').slice(0, 12)}</span>
              </p>
              <div className="flex flex-wrap gap-1">
                {(detail.versions || []).map((version) => (
                  <button
                    key={version.version}
                    type="button"
                    onClick={() => openPublication(detail.id, version.version)}
                    className={`rounded-full border px-2 py-0.5 text-xs ${
                      version.version === detail.selected_version
                        ? 'border-emerald-600 text-emerald-300'
                        : 'border-zinc-700 text-zinc-300'
                    }`}
                  >
                    v{version.version}
                  </button>
                ))}
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  data-testid="catalog-like"
                  disabled={busy}
                  onClick={() => toggleLike(detail)}
                  className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
                >
                  {detail.liked ? 'Curtido' : 'Curtir'} · {detail.like_count}
                </button>
                <button
                  type="button"
                  data-testid="catalog-remix"
                  disabled={busy}
                  onClick={() => remix(detail)}
                  className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
                >
                  Remix v{detail.selected_version}
                </button>
                <button
                  type="button"
                  data-testid="catalog-play"
                  disabled={busy}
                  onClick={() => play(detail)}
                  className="rounded-lg bg-emerald-700 px-3 py-1.5 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
                >
                  Jogar v{detail.selected_version}
                </button>
                {canDelete && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => setRemoving(true)}
                    className="rounded-lg border border-red-900 px-3 py-1.5 text-sm text-red-300 hover:bg-red-950 disabled:opacity-50"
                  >
                    Apagar
                  </button>
                )}
              </div>
              {remixNote && remixNote.publicationId === detail.id && (
                <p className="text-sm text-zinc-400">
                  Scenario {remixNote.scenarioId} · publicação {remixNote.publicationId} v{remixNote.version}
                </p>
              )}
              {removing && (
                <div className="rounded-lg border border-red-900 bg-red-950/40 p-3">
                  <p className="text-sm text-red-100">Apagar «{detail.title}»? As versões saem do catálogo.</p>
                  <div className="mt-2 flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => setRemoving(false)}
                      className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm"
                    >
                      Cancelar
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => remove(detail)}
                      className="rounded-lg bg-red-700 px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
                    >
                      Apagar publicação
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>
      </div>

      <form onSubmit={publish} className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">Publicar</h2>
        <div className="grid gap-2 sm:grid-cols-[9rem_1fr_1fr_auto]">
          <select
            value={publishForm.kind}
            onChange={(event) => setPublishForm((form) => ({ ...form, kind: event.target.value }))}
            aria-label="Tipo"
            className={inputCls}
          >
            <option value="scenario">Scenario</option>
            <option value="adventure">Aventura</option>
          </select>
          <input
            value={publishForm.source_id}
            onChange={(event) => setPublishForm((form) => ({ ...form, source_id: event.target.value }))}
            placeholder="Id da origem"
            aria-label="Id da origem"
            className={inputCls}
          />
          <input
            value={publishForm.cover_image}
            onChange={(event) => setPublishForm((form) => ({ ...form, cover_image: event.target.value }))}
            placeholder="URL da capa (opcional)"
            aria-label="Capa"
            className={inputCls}
          />
          <button
            type="submit"
            disabled={busy || !publishForm.source_id.trim()}
            className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            Publicar
          </button>
        </div>
      </form>
      {playTarget && (
        <PublicationPlayModal publication={playTarget} onClose={() => setPlayTarget(null)} />
      )}
    </div>
  )
}
