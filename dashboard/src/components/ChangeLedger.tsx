import { useState } from 'react'
import type { PendingChanges } from '@/lib/types'
import { parseCommit } from '@/lib/utils'

const COMMITS_SHOWN = 3

interface ChangeLedgerProps {
  changes: PendingChanges
  services?: string[]
}

/** Pending commits and changed files as a two-column mini ledger. */
export function ChangeLedger({ changes, services }: ChangeLedgerProps) {
  const [all, setAll] = useState(false)
  const shown = all ? changes.commits : changes.commits.slice(0, COMMITS_SHOWN)
  const rest = changes.commits.length - shown.length

  return (
    <div className="dl">
      {services && services.length > 0 && (
        <>
          <span className="dl-k">Services</span>
          <div className="dl-v chips">
            {services.map((name) => (
              <span className="tag" key={name} title={name}>
                {name}
              </span>
            ))}
          </div>
        </>
      )}
      <span className="dl-k">Commits</span>
      <div className="dl-v">
        <ul className="commits">
          {shown.map((line) => {
            const [hash, message] = parseCommit(line)
            return (
              <li key={line}>
                <span className="commit-hash">{hash}</span>
                <span className="commit-msg">{message}</span>
              </li>
            )
          })}
        </ul>
        {rest > 0 && (
          <button type="button" className="link-btn" onClick={() => setAll(true)}>
            {rest} more commit{rest > 1 ? 's' : ''}
          </button>
        )}
      </div>
      {changes.stat && (
        <>
          <span className="dl-k">Files</span>
          <div className="dl-v">
            <pre className="files scroll scroll-x">{changes.stat}</pre>
          </div>
        </>
      )}
    </div>
  )
}
