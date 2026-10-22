import { useCallback, useEffect, useState } from 'react'
import { Link, Navigate } from 'react-router-dom'
import { api } from '../api.js'
import { useToast } from '../components/Toast.jsx'

const RULES_MAX = 4000

const inputCls =
  'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600'

const emptyForm = { name: '', description: '', rules: '' }

// Administração dos rulesets de personagem. Só o admin entra.
export default function Rulesets() {
  const toast = useToast()
  const [me, setMe] = useState(null)
  const [rulesets, setRulesets] = useState(null)
  const [form, setForm] = useState(emptyForm)
  const [editingId, setEditingId] = useState(null)
  const [saving, setSaving] = useState(false)
  const [removing, setRemoving] = useState(null)
  const [ready, setReady] = useState(false)

  const load = useCallback(async () => {
    const session = await api.checkAuth()
    setMe(session)
    if (!session?.admin) return
    setRulesets(await api.listRulesets())
  }, [])

  useEffect(() => {
    load()
      .catch((e) => {
        toast(e.message)
        setRulesets((list) => list ?? [])
      })
      .finally(() => setReady(true))
  }, [load, toast])

  const setField = (key) => (e) => {
    const value = key === 'rules' ? e.target.value.slice(0, RULES_MAX) : e.target.value
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  const startEdit = (ruleset) => {
    setRemoving(null)
    setEditingId(ruleset.id)
    setForm({
      name: ruleset.name || '',
      description: ruleset.description || '',
      rules: (ruleset.rules || '').slice(0, RULES_MAX),
    })
  }

  const cancelEdit = () => {
    setEditingId(null)
    setForm(emptyForm)
  }

  const save = async () => {
    const name = form.name.trim()
    const description = form.description.trim()
    const rules = form.rules.slice(0, RULES_MAX)
    if (!name || !rules.trim()) {
      toast('Nome e regras são obrigatórios.')
      return
    }
    setSaving(true)
    try {
      if (editingId) {
        await api.updateRuleset(editingId, { name, description, rules })
        toast('Ruleset atualizado.', 'ok')
      } else {
        await api.createRuleset({ name, description, rules })
        toast('Ruleset criado.', 'ok')
      }
      cancelEdit()
      setRulesets(await api.listRulesets())
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!removing) return
    try {
      await api.deleteRuleset(removing.id)
      if (editingId === removing.id) cancelEdit()
      setRemoving(null)
      setRulesets(await api.listRulesets())
      toast('Ruleset apagado.', 'ok')
    } catch (e) {
      toast(e.message)
    }
  }

  if (!ready) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-zinc-500">
        Carregando…
      </div>
    )
  }
  if (!me?.admin) return <Navigate to="/" replace />

  return (
    <div className="mx-auto flex h-full max-w-2xl flex-col gap-6 overflow-y-auto px-4 py-8 text-zinc-100">
      <div>
        <Link to="/" className="text-sm text-emerald-400 hover:underline">← Aventuras</Link>
        <h1 className="mt-2 text-2xl font-bold">Regras</h1>
        {me && (
          <p className="text-sm text-zinc-500">
            Conjuntos de regras de personagem. Entrou como {me.user} · admin
          </p>
        )}
      </div>

      <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">
          {editingId ? 'Editar ruleset' : 'Novo ruleset'}
        </h2>
        <div className="flex flex-col gap-2">
          <label className="block">
            <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">Nome</span>
            <input
              value={form.name}
              onChange={setField('name')}
              placeholder="Ex.: D&D 5e"
              maxLength={120}
              className={inputCls}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">Descrição</span>
            <input
              value={form.description}
              onChange={setField('description')}
              placeholder="Opcional"
              maxLength={2000}
              className={inputCls}
            />
          </label>
          <label className="block">
            <span className="mb-1 flex items-center justify-between gap-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Regras</span>
              <span className={`text-xs ${form.rules.length >= RULES_MAX ? 'text-red-400' : 'text-zinc-500'}`}>
                {form.rules.length}/{RULES_MAX}
              </span>
            </span>
            <textarea
              value={form.rules}
              onChange={setField('rules')}
              rows={10}
              placeholder="Criação de personagem, testes, combate, magia, morte…"
              className={`${inputCls} resize-y`}
            />
          </label>
          <div className="flex justify-end gap-2">
            {editingId && (
              <button
                type="button"
                onClick={cancelEdit}
                className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800"
              >
                Cancelar
              </button>
            )}
            <button
              type="button"
              onClick={save}
              disabled={saving || form.name.trim().length < 1 || form.rules.trim().length < 1}
              className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
            >
              {saving ? 'Salvando…' : editingId ? 'Salvar' : 'Criar'}
            </button>
          </div>
        </div>
      </section>

      <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
        <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-zinc-500">Lista</h2>
        <p className="mb-3 text-sm text-zinc-500">
          Apagar um ruleset não altera aventuras que já copiaram as regras.
        </p>
        {rulesets === null ? (
          <p className="text-sm text-zinc-500">Carregando…</p>
        ) : rulesets.length === 0 ? (
          <p className="text-sm text-zinc-500">Nenhum ruleset cadastrado.</p>
        ) : (
          <ul className="divide-y divide-zinc-800">
            {rulesets.map((ruleset) => (
              <li key={ruleset.id} className="flex items-start justify-between gap-2 py-3">
                <div className="min-w-0">
                  <div className="font-medium">
                    {ruleset.name}
                    {ruleset.builtin ? ' · padrão' : ''}
                  </div>
                  {ruleset.description ? (
                    <p className="text-sm text-zinc-400">{ruleset.description}</p>
                  ) : (
                    <p className="text-xs text-zinc-600">Sem descrição</p>
                  )}
                </div>
                <div className="flex shrink-0 gap-2">
                  <button
                    type="button"
                    onClick={() => startEdit(ruleset)}
                    className="rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
                  >
                    Editar
                  </button>
                  <button
                    type="button"
                    onClick={() => setRemoving(ruleset)}
                    className="rounded-lg border border-red-900 px-3 py-1.5 text-xs text-red-300 hover:bg-red-950"
                  >
                    Apagar
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {removing && (
        <div className="rounded-xl border border-red-900 bg-red-950/40 p-4">
          <p className="text-sm text-red-100">
            Apagar «{removing.name}»? Aventuras que já usam estas regras não são alteradas.
          </p>
          <div className="mt-3 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setRemoving(null)}
              className="rounded-lg border border-zinc-700 px-3 py-2 text-sm"
            >
              Cancelar
            </button>
            <button
              type="button"
              onClick={remove}
              className="rounded-lg bg-red-700 px-3 py-2 text-sm font-semibold text-white"
            >
              Apagar ruleset
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
