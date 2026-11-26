import type { ReactNode } from 'react'
import { Icon } from './Icon'
import { Mark } from './Status'
import { cx } from '@/lib/utils'

export function Metric({ label, value }: { label?: string; value?: string | number }) {
  return (
    <div className="r-metric">
      {label && value !== undefined && <span className="m-label">{label}</span>}
      {value !== undefined && <span className="m-num">{value}</span>}
    </div>
  )
}

interface RowProps {
  open: boolean
  /** Set when the row can expand. Rows without it show a static title. */
  onToggle?: () => void
  group?: boolean
  title: ReactNode
  meta?: ReactNode
  actions?: ReactNode
  metric: ReactNode
  body?: ReactNode
}

/** One ledger row: chevron + title/meta, actions, number column, optional body. */
export function Row({ open, onToggle, group, title, meta, actions, metric, body }: RowProps) {
  const text = (
    <span className="r-text">
      <span className="r-title">{title}</span>
      {meta && <span className="r-meta">{meta}</span>}
    </span>
  )
  return (
    <div className={cx('row', group && 'is-group')} data-open={open ? '' : undefined}>
      {onToggle ? (
        <button type="button" className="r-toggle" aria-expanded={open} onClick={onToggle}>
          <Icon name="chev" small className="r-chev" />
          {text}
        </button>
      ) : (
        <div className="r-toggle static">
          <span />
          {text}
        </div>
      )}
      <div className="r-actions">{actions}</div>
      {metric}
      {body && <div className="r-body">{body}</div>}
    </div>
  )
}

export function FetchError({ message }: { message: string }) {
  return (
    <>
      <Mark kind="fail" label="Fetch error" />
      <span className="mono">{message}</span>
    </>
  )
}

interface IconActionProps {
  icon: Parameters<typeof Icon>[0]['name']
  label: string
  title?: string
  pressed?: boolean
  onClick: () => void
}

export function IconAction({ icon, label, title, pressed, onClick }: IconActionProps) {
  return (
    <button type="button" className="icon-btn" aria-label={label} title={title ?? label} aria-pressed={pressed} onClick={onClick}>
      <Icon name={icon} />
    </button>
  )
}
