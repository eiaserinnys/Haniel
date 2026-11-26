// Repo rows: Haniel itself, a repo with services (group), and a repo without services.

import { useState } from 'react'
import { getSelfLogs } from '@/lib/api'
import type { NamedRepo, RepoServiceGroup } from '@/lib/groups'
import type { RepoStatus, SelfUpdateStatus } from '@/lib/types'
import { ChangeLedger } from './ChangeLedger'
import { LogViewer } from './LogViewer'
import { FetchError, IconAction, Metric, Row } from './Row'
import { Mark } from './Status'

const headText = (repo: RepoStatus) => `${repo.branch} ${repo.last_head?.slice(0, 8) ?? '—'}`

function RepoTitle({ name, repo, tag }: { name: string; repo: RepoStatus; tag?: boolean }) {
  return (
    <>
      <span className="r-name" title={name}>
        {name}
      </span>
      {tag && <span className="tag tag-accent">self</span>}
      <span className="r-branch" title={repo.branch}>
        {headText(repo)}
      </span>
    </>
  )
}

const pendingMetric = (repo: RepoStatus) =>
  repo.pending_changes ? <Metric label="pending" value={repo.pending_changes.commits.length} /> : <div className="r-metric" />

interface SelfRowProps {
  repo: NamedRepo
  selfUpdate: SelfUpdateStatus
  onApprove: () => void
  onRestart: () => void
}

export function SelfRow({ repo, selfUpdate, onApprove, onRestart }: SelfRowProps) {
  const [open, setOpen] = useState(false)
  const [showLogs, setShowLogs] = useState(false)
  const { repoName, repo: status } = repo

  return (
    <Row
      open={open}
      onToggle={status.pending_changes ? () => setOpen((v) => !v) : undefined}
      title={<RepoTitle name={repoName} repo={status} tag />}
      meta={status.fetch_error ? <FetchError message={status.fetch_error} /> : undefined}
      actions={
        <>
          <span className="icon-group">
            <IconAction icon="logs" label="Haniel logs" title="Logs" pressed={showLogs} onClick={() => setShowLogs((v) => !v)} />
            <IconAction icon="restart" label="Restart Haniel" onClick={onRestart} />
          </span>
          {selfUpdate.pending && (
            <button type="button" className="btn btn-primary" onClick={onApprove}>
              Approve
            </button>
          )}
        </>
      }
      metric={pendingMetric(status)}
      body={
        (open && status.pending_changes) || showLogs ? (
          <>
            {open && status.pending_changes && <ChangeLedger changes={status.pending_changes} />}
            {showLogs && <LogViewer serviceName="haniel" fetchFn={getSelfLogs} />}
          </>
        ) : undefined
      }
    />
  )
}

interface RepoActionProps {
  repoName: string
  repo: RepoStatus
  isPulling: boolean
  onPull: (repoName: string) => void
}

function PullAction({ repoName, repo, isPulling, onPull }: RepoActionProps) {
  if (isPulling) return <Mark kind="busy" label="Pulling…" />
  if (!repo.pending_changes) return null
  return (
    <button type="button" className="btn btn-outline" aria-label={`Deploy ${repoName}`} onClick={() => onPull(repoName)}>
      Deploy
    </button>
  )
}

export function RepoGroupRow({ group, isPulling, onPull }: { group: RepoServiceGroup; isPulling: boolean; onPull: (repoName: string) => void }) {
  const [open, setOpen] = useState(false)
  const { repoName, repo, services } = group

  return (
    <Row
      group
      open={open}
      onToggle={repo.pending_changes ? () => setOpen((v) => !v) : undefined}
      title={<RepoTitle name={repoName} repo={repo} />}
      meta={repo.fetch_error ? <FetchError message={repo.fetch_error} /> : undefined}
      actions={<PullAction repoName={repoName} repo={repo} isPulling={isPulling} onPull={onPull} />}
      metric={pendingMetric(repo)}
      body={open && repo.pending_changes ? <ChangeLedger changes={repo.pending_changes} services={services.map((s) => s.name)} /> : undefined}
    />
  )
}

interface RepoRowProps {
  repo: NamedRepo
  isPulling: boolean
  onPull: (repoName: string) => void
  onEdit: (repoName: string) => void
  onDelete: (repoName: string) => void
}

export function RepoRow({ repo, isPulling, onPull, onEdit, onDelete }: RepoRowProps) {
  const [open, setOpen] = useState(false)
  const { repoName, repo: status } = repo

  return (
    <Row
      open={open}
      onToggle={status.pending_changes ? () => setOpen((v) => !v) : undefined}
      title={<RepoTitle name={repoName} repo={status} />}
      meta={status.fetch_error ? <FetchError message={status.fetch_error} /> : undefined}
      actions={
        <>
          <PullAction repoName={repoName} repo={status} isPulling={isPulling} onPull={onPull} />
          <span className="icon-group">
            <IconAction icon="edit" label={`Edit ${repoName}`} title="Edit" onClick={() => onEdit(repoName)} />
            <IconAction icon="trash" label={`Delete ${repoName}`} title="Delete" onClick={() => onDelete(repoName)} />
          </span>
        </>
      }
      metric={pendingMetric(status)}
      body={open && status.pending_changes ? <ChangeLedger changes={status.pending_changes} /> : undefined}
    />
  )
}
