// ServiceCard — displays one service with controls
// Uses Phase 1 API ServiceStatus shape: { state, uptime, restart_count, consecutive_failures, config }

import { useState } from 'react'
import { ChevronDown, ChevronRight, Play, Square, RotateCcw, Zap, Terminal } from 'lucide-react'
import type { ServiceStatus } from '@/lib/types'
import { StatusBadge } from './StatusBadge'
import { LogViewer } from './LogViewer'
import { cn, formatUptime } from '@/lib/utils'

interface ServiceCardProps {
  name: string
  service: ServiceStatus
  onControl: (name: string, action: 'start' | 'stop' | 'restart' | 'enable') => void
}

export function ServiceCard({ name, service, onControl }: ServiceCardProps) {
  const [expanded, setExpanded] = useState(false)
  const [showLogs, setShowLogs] = useState(false)

  const { state, uptime, restart_count, consecutive_failures, config } = service
  const isRunning = state === 'running' || state === 'ready'
  const canStart  = state === 'stopped' || state === 'crashed'
  const canStop   = isRunning || state === 'starting'
  const canEnable = state === 'circuit_open'

  return (
    <div className="rounded-lg border border-zinc-700 bg-zinc-800/50 overflow-hidden">
      {/* Header row */}
      <div className="flex items-center gap-3 p-4">
        <button
          className="text-zinc-500 hover:text-zinc-300 transition-colors shrink-0"
          onClick={() => setExpanded((v) => !v)}
          aria-label={expanded ? 'Collapse' : 'Expand'}
        >
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </button>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-zinc-100">{name}</span>
            <StatusBadge state={state} />
            {consecutive_failures > 0 && (
              <span className="text-xs text-red-400">
                {consecutive_failures} fail{consecutive_failures !== 1 ? 's' : ''}
              </span>
            )}
          </div>
          <p className="mt-0.5 text-xs text-zinc-500 truncate font-mono">{config.run}</p>
        </div>

        <div className="flex items-center gap-1 shrink-0">
          <span className="text-xs text-zinc-500 mr-1">{formatUptime(uptime)}</span>

          {canStart && (
            <ActionButton
              icon={<Play size={14} />}
              label="Start"
              onClick={() => onControl(name, 'start')}
              className="text-green-400 hover:text-green-300"
            />
          )}
          {canStop && (
            <ActionButton
              icon={<Square size={14} />}
              label="Stop"
              onClick={() => onControl(name, 'stop')}
              className="text-red-400 hover:text-red-300"
            />
          )}
          {isRunning && (
            <ActionButton
              icon={<RotateCcw size={14} />}
              label="Restart"
              onClick={() => onControl(name, 'restart')}
              className="text-yellow-400 hover:text-yellow-300"
            />
          )}
          {canEnable && (
            <ActionButton
              icon={<Zap size={14} />}
              label="Enable (reset circuit)"
              onClick={() => onControl(name, 'enable')}
              className="text-orange-400 hover:text-orange-300"
            />
          )}
          <ActionButton
            icon={<Terminal size={14} />}
            label="Logs"
            onClick={() => setShowLogs((v) => !v)}
            className={cn(
              'text-zinc-400 hover:text-zinc-300',
              showLogs && 'text-zinc-200',
            )}
          />
        </div>
      </div>

      {/* Expanded details */}
      {expanded && (
        <div className="border-t border-zinc-700 px-4 py-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
          <Detail label="Restarts" value={restart_count.toString()} />
          <Detail label="Uptime"   value={formatUptime(uptime)} />
          {config.cwd  && <Detail label="CWD"  value={config.cwd}  mono className="col-span-2" />}
          {config.repo && <Detail label="Repo" value={config.repo} />}
          {config.after.length > 0 && (
            <Detail label="Depends on" value={config.after.join(', ')} className="col-span-2" />
          )}
        </div>
      )}

      {/* Log viewer */}
      {showLogs && (
        <div className="border-t border-zinc-700">
          <LogViewer serviceName={name} />
        </div>
      )}
    </div>
  )
}

// ── Helpers ──────────────────────────────────────────────────────────────────

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

interface DetailProps {
  label: string
  value: string
  mono?: boolean
  className?: string
}

function Detail({ label, value, mono, className }: DetailProps) {
  return (
    <div className={cn('flex flex-col gap-0.5', className)}>
      <span className="text-zinc-500">{label}</span>
      <span className={cn('text-zinc-300', mono && 'font-mono truncate')}>{value}</span>
    </div>
  )
}
