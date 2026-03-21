// HanielSelfCard — Self card with logs and restart controls

import { useState } from 'react'
import { Terminal, RotateCcw } from 'lucide-react'
import { cn } from '@/lib/utils'
import { getSelfLogs } from '@/lib/api'
import { LogViewer } from './LogViewer'
import type { NamedRepo } from '@/lib/groups'
import type { SelfUpdateStatus } from '@/lib/types'

interface HanielSelfCardProps {
  repo: NamedRepo
  selfUpdate: SelfUpdateStatus
  onApprove: () => void
  onRestart: () => void
}

export function HanielSelfCard({ repo, selfUpdate, onApprove, onRestart }: HanielSelfCardProps) {
  const [showLogs, setShowLogs] = useState(false)
  const pending = selfUpdate?.pending
  const { repoName, repo: repoStatus } = repo

  const handleRestart = () => {
    if (window.confirm('Restarting Haniel will temporarily stop all services.\nAre you sure you want to restart?')) {
      onRestart()
    }
  }

  return (
    <div className="rounded-lg border border-blue-700/50 bg-zinc-800/50 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3">
        <div className="flex items-center gap-2 min-w-0">
          <span className="font-medium text-zinc-200">{repoName}</span>
          <span className="text-xs px-2 py-0.5 rounded-full bg-blue-500/20 text-blue-400 border border-blue-500/30">
            self
          </span>
          <span className="text-xs text-zinc-500 font-mono">
            {repoStatus.branch} · {repoStatus.last_head?.slice(0, 8) ?? '—'}
          </span>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <ActionButton
            icon={<Terminal size={14} />}
            label="Logs"
            onClick={() => setShowLogs((v) => !v)}
            className={cn(
              'text-zinc-400 hover:text-zinc-300',
              showLogs && 'text-zinc-200',
            )}
          />
          <ActionButton
            icon={<RotateCcw size={14} />}
            label="Restart haniel"
            onClick={handleRestart}
            className="text-yellow-400 hover:text-yellow-300"
          />
          {pending && (
            <button
              onClick={onApprove}
              className="text-xs bg-blue-600 hover:bg-blue-500 text-white px-3 py-1 rounded transition-colors ml-1"
            >
              Approve update
            </button>
          )}
        </div>
      </div>

      {repoStatus.fetch_error && (
        <div className="px-4 pb-2 text-xs text-red-400">
          Fetch error: {repoStatus.fetch_error}
        </div>
      )}

      {repoStatus.pending_changes && (
        <div className="border-t border-blue-700/30 px-4 py-2">
          <details className="text-xs text-zinc-400">
            <summary className="cursor-pointer hover:text-zinc-300">
              {repoStatus.pending_changes.commits.length} pending commits
            </summary>
            <pre className="mt-1 bg-zinc-900 rounded p-2 overflow-x-auto">
              {repoStatus.pending_changes.commits.join('\n')}
            </pre>
            {repoStatus.pending_changes.stat && (
              <pre className="mt-1 bg-zinc-900 rounded p-2 overflow-x-auto">
                {repoStatus.pending_changes.stat}
              </pre>
            )}
          </details>
        </div>
      )}

      {showLogs && (
        <div className="border-t border-blue-700/30">
          <LogViewer serviceName="haniel" fetchFn={getSelfLogs} />
        </div>
      )}
    </div>
  )
}

interface ActionButtonProps {
  icon: React.ReactNode
  label: string
  onClick: () => void
  className?: string
}

function ActionButton({ icon, label, onClick, className }: ActionButtonProps) {
  return (
    <button
      title={label}
      onClick={onClick}
      className={cn('p-1.5 rounded transition-colors hover:bg-zinc-700/50', className)}
    >
      {icon}
    </button>
  )
}
