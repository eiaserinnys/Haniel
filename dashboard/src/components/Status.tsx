import type { ServiceState } from '@/lib/types'

export type MarkKind = 'ok' | 'busy' | 'wait' | 'off' | 'fail'

export function Mark({ kind, label }: { kind: MarkKind; label: string }) {
  return <span className={`st st-${kind}`}>{label}</span>
}

const SERVICE_MARKS: Record<ServiceState | 'disabled', [MarkKind, string]> = {
  running: ['ok', 'Running'],
  ready: ['ok', 'Ready'],
  starting: ['busy', 'Starting'],
  stopping: ['busy', 'Stopping'],
  stopped: ['off', 'Stopped'],
  crashed: ['fail', 'Crashed'],
  circuit_open: ['fail', 'Circuit open'],
  disabled: ['off', 'Disabled'],
}

export function ServiceMark({ state, enabled }: { state: ServiceState; enabled: boolean }) {
  const key = !enabled && state === 'stopped' ? 'disabled' : state
  const [kind, label] = SERVICE_MARKS[key] ?? (['off', state] as [MarkKind, string])
  return <Mark kind={kind} label={label} />
}
