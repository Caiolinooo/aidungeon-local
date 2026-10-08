import { fmtDate } from '../../utils.js'

// Aba Detalhes: metadados da aventura (título, descrição, tags, datas).
// Somente leitura: o contrato v1 não tem PATCH /api/adventures/:id.
export default function DetailsTab({ adventure }) {
  const rows = [
    ['Source', adventure.source === 'import' ? 'Imported backup' : 'New'],
    ['Actions', adventure.actions?.length ?? '—'],
    ['Story cards', adventure.story_cards?.length ?? '—'],
    ['Memories', adventure.memories?.length ?? '—'],
    ['Third person', adventure.third_person ? 'Yes' : 'No (2nd person)'],
    ['Created', fmtDate(adventure.created_at)],
    ['Updated', fmtDate(adventure.updated_at)],
  ]

  return (
    <div className="flex flex-col gap-4">
      <div>
        <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">Title</span>
        <p className="text-lg font-semibold text-zinc-100">{adventure.title}</p>
      </div>

      <div>
        <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">Description</span>
        <p className="whitespace-pre-wrap text-sm text-zinc-400">{adventure.description || '—'}</p>
      </div>

      <div>
        <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">Tags</span>
        <div className="flex flex-wrap gap-1.5">
          {(adventure.tags ?? []).length === 0 && <span className="text-sm text-zinc-600">—</span>}
          {(adventure.tags ?? []).map((t) => (
            <span key={t} className="rounded-full bg-zinc-700 px-2.5 py-0.5 text-xs text-zinc-200">
              {t}
            </span>
          ))}
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between gap-2 border-b border-zinc-800/60 pb-1">
            <dt className="text-zinc-500">{k}</dt>
            <dd className="text-right text-zinc-300">{v}</dd>
          </div>
        ))}
      </dl>

      <a
        href={`/api/adventures/${adventure.id}/export`}
        download
        className="mt-2 rounded-lg border border-zinc-700 px-4 py-2 text-center text-sm text-zinc-300 hover:bg-zinc-800"
      >
        ⬇ Export backup (.zip — AI Dungeon format)
      </a>

      <p className="text-xs text-zinc-600">
        Read-only in v1: the API contract has no adventure update endpoint.
      </p>
    </div>
  )
}
