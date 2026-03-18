// ServiceList — renders grid of ServiceCards
// Uses Phase 1 API RunnerStatus shape

import { ServiceCard } from './ServiceCard'
import type { RunnerStatus } from '@/lib/types'

interface ServiceListProps {
  status: RunnerStatus
  onControl: (name: string, action: 'start' | 'stop' | 'restart' | 'enable') => void
  onEdit?: (name: string) => void
  onDelete?: (name: string) => void
}

export function ServiceList({ status, onControl, onEdit, onDelete }: ServiceListProps) {
  const entries = Object.entries(status.services).sort(([a], [b]) => a.localeCompare(b))

  if (entries.length === 0) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-8 text-center text-zinc-500 text-sm">
        No services configured.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      {entries.map(([name, svc]) => (
        <ServiceCard
          key={name}
          name={name}
          service={svc}
          onControl={onControl}
          onEdit={onEdit}
          onDelete={onDelete}
        />
      ))}
    </div>
  )
}
