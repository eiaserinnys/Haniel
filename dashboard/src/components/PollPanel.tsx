import { useState } from 'react'
import { Icon } from './Icon'
import { Mark } from './Status'
import { useNextPollCountdown } from '@/hooks/useNextPollCountdown'
import type { RunnerStatus } from '@/lib/types'
import { formatClock } from '@/lib/utils'

export function PollPanel({ status, onFetch }: { status: RunnerStatus; onFetch: () => Promise<void> }) {
  const [fetching, setFetching] = useState(false)
  const next = useNextPollCountdown(status.last_poll, status.poll_interval)

  const fetchNow = async () => {
    if (fetching) return
    setFetching(true)
    try {
      await onFetch()
    } finally {
      setFetching(false)
    }
  }

  return (
    <div className="side-foot">
      <div className="side-kv">
        <span>Last poll</span>
        <span className="mono">{formatClock(status.last_poll)}</span>
      </div>
      <div className="side-kv">
        <span>Next in</span>
        <span className="mono">{next === null ? '—' : `${next}s`}</span>
      </div>
      <div className="side-kv">
        <span>Interval</span>
        <span className="mono">{status.poll_interval}s</span>
      </div>
      <div className="side-kv">
        <span>Polls</span>
        <span className="mono">{status.poll_count.toLocaleString('en-US')}</span>
      </div>
      {status.pending_restarts.length > 0 && (
        <div className="side-restart">
          <Mark kind="wait" label="Restart pending" />
          <span className="mono">{status.pending_restarts.join(', ')}</span>
        </div>
      )}
      <button type="button" className="btn btn-ghost" aria-disabled={fetching || undefined} onClick={fetchNow}>
        <Icon name="reload" small className={fetching ? 'spin' : undefined} />
        Fetch now
      </button>
    </div>
  )
}
