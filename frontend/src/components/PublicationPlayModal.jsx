import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '../api.js'
import Modal from '../components/Modal.jsx'
import { useToast } from './Toast.jsx'
import { storyStartsFromScenario } from '../api/storyStarts.js'

export default function PublicationPlayModal({ publication, onClose }) {
  const navigate = useNavigate()
  const toast = useToast()
  const starts = storyStartsFromScenario(publication.snapshot)
  const [startId, setStartId] = useState(starts[0]?.id || '')
  const [creating, setCreating] = useState(false)

  const start = async () => {
    setCreating(true)
    try {
      const adventure = await api.createAdventure({
        publication_id: publication.id,
        publication_version: publication.selected_version,
        ...(startId ? { start_id: startId } : {}),
      })
      navigate(`/play/${adventure.id}`)
    } catch (error) {
      toast(error.message)
      setCreating(false)
    }
  }

  return (
    <Modal title={`Jogar ${publication.title}`} onClose={onClose}>
      <div className="flex flex-col gap-4">
        <p className="text-sm text-zinc-400">
          Versão {publication.selected_version}. A aventura começa do snapshot publicado, sem ações ou jogadores do original.
        </p>
        {starts.length > 0 && (
          <div className="flex flex-col gap-2" role="radiogroup" aria-label="Início da história">
            {starts.map((item) => (
              <label key={item.id} className="flex cursor-pointer gap-3 rounded-lg border border-zinc-700 p-3">
                <input type="radio" name="publication-start" checked={startId === item.id}
                  onChange={() => setStartId(item.id)} />
                <span>
                  <span className="block text-sm font-medium">{item.title}</span>
                  <span className="line-clamp-3 text-xs text-zinc-400">{item.text}</span>
                </span>
              </label>
            ))}
          </div>
        )}
        <button type="button" onClick={start} disabled={creating}
          className="self-end rounded-lg bg-emerald-700 px-5 py-2 text-sm font-semibold hover:bg-emerald-600 disabled:opacity-50">
          {creating ? 'Criando…' : 'Começar aventura'}
        </button>
      </div>
    </Modal>
  )
}
