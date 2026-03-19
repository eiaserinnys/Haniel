// RepoServiceGroup — Repo header + associated service cards

import { Package } from 'lucide-react'
import type { RepoServiceGroup as RepoServiceGroupType } from '@/lib/groups'
import { ServiceCard } from './ServiceCard'

interface RepoServiceGroupProps {
  group: RepoServiceGroupType
  onControl: (name: string, action: 'start' | 'stop' | 'restart' | 'enable') => void
  onPull: (repoName: string) => void
  isPulling: boolean
  onEdit?: (name: string) => void
  onDelete?: (name: string) => void
}

export function RepoServiceGroup({
  group,
  onControl,
  onPull,
  isPulling,
  onEdit,
  onDelete,
}: RepoServiceGroupProps) {
  const { repoName, repo, services } = group

  return (
    <div className="rounded-lg border border-zinc-700 bg-zinc-800/50 overflow-hidden">
      {/* Repo header */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-zinc-800/30 border-b border-zinc-700">
        <div className="flex items-center gap-2 min-w-0">
          <Package size={14} className="text-zinc-500 shrink-0" />
          <span className="font-medium text-zinc-400">{repoName}</span>
          <span className="text-xs text-zinc-500 font-mono">
            {repo.branch} · {repo.last_head?.slice(0, 8) ?? '—'}
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {isPulling ? (
            <span className="text-xs text-blue-400/60 px-2 py-1 animate-pulse">
              Pulling…
            </span>
          ) : repo.pending_changes && (
            <button
              onClick={() => onPull(repoName)}
              className="text-xs bg-blue-600/20 text-blue-400 hover:bg-blue-600/40 px-2 py-1 rounded transition-colors"
            >
              Update ({repo.pending_changes.commits.length} commits)
            </button>
          )}
        </div>
      </div>

      {repo.fetch_error && (
        <div className="px-4 py-1.5 text-xs text-red-400">
          Fetch error: {repo.fetch_error}
        </div>
      )}

      {/* Service rows */}
      {services.map(({ name, service }, idx) => (
        <div key={name} className={idx > 0 ? 'border-t border-zinc-700' : ''}>
          <ServiceCard
            name={name}
            service={service}
            onControl={onControl}
            onEdit={onEdit}
            onDelete={onDelete}
            hideRepo
            noBorder
          />
        </div>
      ))}

      {/* Pending commits details */}
      {repo.pending_changes && (
        <div className="border-t border-zinc-700 px-4 py-2">
          <details className="text-xs text-zinc-400">
            <summary className="cursor-pointer hover:text-zinc-300">
              {repo.pending_changes.commits.length} pending commits
            </summary>
            <pre className="mt-1 bg-zinc-900 rounded p-2 overflow-x-auto">
              {repo.pending_changes.commits.join('\n')}
            </pre>
            {repo.pending_changes.stat && (
              <pre className="mt-1 bg-zinc-900 rounded p-2 overflow-x-auto">
                {repo.pending_changes.stat}
              </pre>
            )}
          </details>
        </div>
      )}
    </div>
  )
}
