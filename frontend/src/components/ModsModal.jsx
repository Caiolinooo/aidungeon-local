import { useCallback, useEffect, useState } from 'react'

import {
  applyMod, disableAdventureMod, enableAdventureMod, listAdventureMods,
  listMods, publishMod, removeAdventureMod,
} from '../api/mods.js'
import Modal from './Modal.jsx'
import { useToast } from './Toast.jsx'

const emptyPackage = JSON.stringify({ cards: [], location_links: [], append: {}, scripts: {} }, null, 2)
const inputCls = 'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100'

export default function ModsModal({ adventureId, onClose }) {
  const toast = useToast()
  const [catalog, setCatalog] = useState([])
  const [applied, setApplied] = useState([])
  const [title, setTitle] = useState('')
  const [packageText, setPackageText] = useState(emptyPackage)
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    const [available, current] = await Promise.all([listMods(), listAdventureMods(adventureId)])
    setCatalog(available.mods || [])
    setApplied(current.mods || [])
  }, [adventureId])
  useEffect(() => { load().catch((error) => toast(error.message)) }, [load, toast])

  const run = async (operation) => {
    setBusy(true)
    try { await operation(); await load() } catch (error) { toast(error.message) } finally { setBusy(false) }
  }
  const publish = (event) => {
    event.preventDefault()
    run(async () => {
      let content
      try { content = JSON.parse(packageText) } catch { throw new Error('JSON do mod inválido.') }
      await publishMod({ title, package: content })
      setTitle('')
      setPackageText(emptyPackage)
      toast('Mod publicado.')
    })
  }

  return (
    <Modal title="Mods da aventura" onClose={onClose} wide>
      <div className="grid gap-5 md:grid-cols-2">
        <section>
          <h3 className="mb-2 text-sm font-semibold">Instalados</h3>
          {applied.length === 0 ? <p className="text-sm text-zinc-500">Nenhum mod aplicado.</p> : (
            <ul className="flex flex-col gap-2">{applied.map((mod) => (
              <li key={mod.mod_version_id} className="rounded-lg border border-zinc-700 p-3">
                <div className="text-sm font-medium">{mod.title} · v{mod.version}</div>
                <div className="mt-2 flex gap-2">
                  <button disabled={busy} onClick={() => run(() => (mod.enabled
                    ? disableAdventureMod(adventureId, mod.mod_version_id)
                    : enableAdventureMod(adventureId, mod.mod_version_id)))}
                    className="text-xs text-emerald-400">{mod.enabled ? 'Desativar append/scripts' : 'Ativar'}</button>
                  <button disabled={busy} onClick={() => run(() => removeAdventureMod(adventureId, mod.mod_version_id))}
                    className="text-xs text-red-400">Remover conteúdo</button>
                </div>
              </li>
            ))}</ul>
          )}
          <h3 className="mb-2 mt-5 text-sm font-semibold">Catálogo de mods</h3>
          <ul className="flex max-h-56 flex-col gap-2 overflow-y-auto">{catalog.map((mod) => {
            const latest = mod.versions?.[mod.versions.length - 1]
            return (
            <li key={mod.id} className="flex items-center justify-between gap-2 rounded-lg border border-zinc-800 p-2">
              <span className="min-w-0"><span className="block truncate text-sm">{mod.title}</span><span className="text-xs text-zinc-500">v{latest?.version || 1}</span></span>
              <button disabled={busy || !latest || applied.some((item) => item.mod_version_id === latest.id)}
                onClick={() => run(() => applyMod(adventureId, latest.id))}
                className="shrink-0 text-xs text-emerald-400 disabled:opacity-40">Aplicar</button>
            </li>
            )
          })}</ul>
        </section>
        <form onSubmit={publish} className="flex flex-col gap-2">
          <h3 className="text-sm font-semibold">Publicar mod</h3>
          <p className="text-xs text-zinc-500">Somente cards, links, append e scripts aditivos. Não envie saves ou jogadores.</p>
          <input required aria-label="Título do mod" placeholder="Título" value={title} onChange={(event) => setTitle(event.target.value)} className={inputCls} />
          <textarea required aria-label="Pacote JSON do mod" value={packageText} onChange={(event) => setPackageText(event.target.value)} rows={16} className={`${inputCls} font-mono text-xs`} />
          <button disabled={busy || !title.trim()} className="self-end rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold disabled:opacity-50">Publicar</button>
        </form>
      </div>
    </Modal>
  )
}
