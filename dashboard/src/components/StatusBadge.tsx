// StatusBadge — color-coded service state badge

import { cn } from '@/lib/utils'
import type { ServiceState } from '@/lib/types'

const STATE_STYLES: Record<ServiceState, string> = {
  running:      'bg-green-500/20 text-green-400 border-green-500/30',
  ready:        'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  starting:     'bg-blue-500/20 text-blue-400 border-blue-500/30',
  stopping:     'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  stopped:      'bg-zinc-500/20 text-zinc-400 border-zinc-500/30',
  crashed:      'bg-red-500/20 text-red-400 border-red-500/30',
  circuit_open: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
}

const STATE_LABELS: Record<ServiceState, string> = {
  running:      'Running',
  ready:        'Ready',
  starting:     'Starting',
  stopping:     'Stopping',
  stopped:      'Stopped',
  crashed:      'Crashed',
  circuit_open: 'Circuit Open',
}

interface StatusBadgeProps {
  state: ServiceState
  className?: string
}

export function StatusBadge({ state, className }: StatusBadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
        STATE_STYLES[state] ?? 'bg-zinc-500/20 text-zinc-400 border-zinc-500/30',
        className,
      )}
    >
      {STATE_LABELS[state] ?? state}
    </span>
  )
}
