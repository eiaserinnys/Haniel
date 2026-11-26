import { Icon } from './Icon'
import { Mark } from './Status'

export function SkeletonRows({ count }: { count: number }) {
  return (
    <>
      {Array.from({ length: count }, (_, i) => (
        <div className="sk-row" aria-hidden="true" key={i}>
          <div className="sk-lines">
            <span className={`sk ${i % 2 ? 'w1' : 'w3'}`} />
            <span className={`sk meta ${i % 2 ? 'w3' : 'w2'}`} />
          </div>
          <span className="sk metric" />
        </div>
      ))}
    </>
  )
}

export function LoadError({ what, detail, onRetry }: { what: string; detail: string; onRetry: () => void }) {
  return (
    <div className="empty" role="alert">
      <Mark kind="fail" label="Error" />
      <p className="empty-title">Couldn't load {what}</p>
      <p className="empty-text mono">{detail}</p>
      <button type="button" className="btn btn-outline" onClick={onRetry}>
        Retry
      </button>
    </div>
  )
}

interface NoticeProps {
  summary?: string
  detail?: string
  onDismiss?: () => void
  onRetry?: () => void
}

export function Notice({ summary, detail, onDismiss, onRetry }: NoticeProps) {
  return (
    <div className="notice" role="alert">
      <Mark kind="fail" label="Error" />
      <p className="notice-text">
        {summary} {detail && <span className="mono sel">{detail}</span>}
      </p>
      {onRetry ? (
        <button type="button" className="btn btn-outline" onClick={onRetry}>
          Retry
        </button>
      ) : onDismiss ? (
        <button type="button" className="icon-btn" aria-label="Dismiss" onClick={onDismiss}>
          <Icon name="x" small />
        </button>
      ) : (
        <span />
      )}
    </div>
  )
}
