import { PollPanel } from './PollPanel'
import type { RunnerStatus } from '@/lib/types'

export type SectionId = 'services' | 'repos' | 'deps'

interface SectionNavProps {
  counts: { services: number | null; repos: number | null }
  current: SectionId
  onJump: (id: SectionId) => void
}

const SECTIONS: Array<[SectionId, string]> = [
  ['services', 'Services'],
  ['repos', 'Repositories'],
  ['deps', 'Dependencies'],
]

export function SectionNav({ counts, current, onJump }: SectionNavProps) {
  return (
    <nav className="nav" aria-label="Sections">
      {SECTIONS.map(([id, label]) => (
        <button key={id} type="button" className="nav-row" aria-current={current === id ? 'true' : undefined} onClick={() => onJump(id)}>
          <span>{label}</span>
          <span className="nav-count">{id === 'deps' ? '' : counts[id]}</span>
        </button>
      ))}
    </nav>
  )
}

interface SidePanelProps extends SectionNavProps {
  status: RunnerStatus | null
  onFetch: () => Promise<void>
}

/** Section list plus the polling lines; shared by the sidebar and the phone drawer. */
export function SidePanel({ status, onFetch, ...nav }: SidePanelProps) {
  return (
    <>
      <SectionNav {...nav} />
      {status && <PollPanel status={status} onFetch={onFetch} />}
    </>
  )
}
