import { useState } from 'react'
import { Link } from 'react-router-dom'

import { publishPublication } from '../api/publications.js'
import Modal from './Modal.jsx'
import { useToast } from './Toast.jsx'

const inputCls = 'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100'

export default function PublicationModal({ adventure, onClose }) {
  const toast = useToast()
  const [form, setForm] = useState({
    title: adventure.title || '', description: adventure.description || '',
    tags: (adventure.tags || []).join(', '), cover_image: '',
  })
  const [busy, setBusy] = useState(false)
  const [published, setPublished] = useState(null)

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    try {
      const result = await publishPublication({
        kind: 'adventure', source_id: adventure.id,
        title: form.title, description: form.description, cover_image: form.cover_image,
        tags: form.tags.split(',').map((tag) => tag.trim()).filter(Boolean),
      })
      setPublished(result)
      toast(result.created ? 'Nova versão publicada.' : 'Este conteúdo já estava publicado.')
    } catch (error) {
      toast(error.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Publicar aventura" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-col gap-3">
        <p className="text-sm text-zinc-400">Publica um snapshot sem ações, memórias, jogadores ou retratos. Cada versão é imutável.</p>
        <input aria-label="Título da publicação" required value={form.title} className={inputCls}
          onChange={(event) => setForm({ ...form, title: event.target.value })} />
        <textarea aria-label="Descrição da publicação" value={form.description} rows={3} className={inputCls}
          onChange={(event) => setForm({ ...form, description: event.target.value })} />
        <input aria-label="Tags da publicação" value={form.tags} placeholder="fantasia, mistério" className={inputCls}
          onChange={(event) => setForm({ ...form, tags: event.target.value })} />
        <input aria-label="URL da capa" value={form.cover_image} placeholder="URL da capa (opcional)" className={inputCls}
          onChange={(event) => setForm({ ...form, cover_image: event.target.value })} />
        {published && <p className="text-sm text-emerald-400">Versão {published.selected_version} publicada. <Link className="underline" to="/catalog">Abrir catálogo</Link></p>}
        <button disabled={busy || !form.title.trim()} className="self-end rounded-lg bg-emerald-700 px-5 py-2 text-sm font-semibold disabled:opacity-50">
          {busy ? 'Publicando…' : 'Publicar versão'}
        </button>
      </form>
    </Modal>
  )
}
