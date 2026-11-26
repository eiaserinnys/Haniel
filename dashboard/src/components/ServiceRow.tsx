// ServiceRow — one service with controls
// Uses Phase 1 API ServiceStatus shape: { state, uptime, restart_count, consecutive_failures, config }

import { useState } from 'react'
import type { ServiceStatus } from '@/lib/types'
import { formatUptime } from '@/lib/utils'
import { IconAction, Metric, Row } from './Row'
import { LogViewer } from './LogViewer'
import { ServiceMark } from './Status'

interface ServiceRowProps {
  name: string
  service: ServiceStatus
  onControl: (name: string, action: 'start' | 'stop' | 'restart' | 'enable') => void
  onEdit: (name: string) => void
  onDelete: (name: string) => void
  /** Hide the Repo detail (used when rendered under its repo group row). */
  hideRepo?: boolean
}

const LIVE_STATES = ['running', 'ready', 'starting', 'stopping']

export function ServiceRow({ name, service, onControl, onEdit, onDelete, hideRepo }: ServiceRowProps) {
  const [expanded, setExpanded] = useState(false)
  const [showLogs, setShowLogs] = useState(false)

  const { state, uptime, restart_count, consecutive_failures, config } = service
  const isRunning = state === 'running' || state === 'ready'
  const canStart = state === 'stopped' || state === 'crashed'
  const canStop = isRunning || state === 'starting'
  const canEnable = state === 'circuit_open'

  return (
    <Row
      open={expanded}
      onToggle={() => setExpanded((v) => !v)}
      title={
        <>
          <span className="r-name" title={name}>
            {name}
          </span>
          <ServiceMark state={state} enabled={config.enabled} />
          {consecutive_failures > 0 && (
            <span className="m-label">
              {consecutive_failures} fail{consecutive_failures !== 1 ? 's' : ''}
            </span>
          )}
        </>
      }
      meta={<span className="r-cmd mono">{config.run}</span>}
      actions={
        <span className="icon-group">
          {canStart && <IconAction icon="play" label={`Start ${name}`} title="Start" onClick={() => onControl(name, 'start')} />}
          {canStop && <IconAction icon="stop" label={`Stop ${name}`} title="Stop" onClick={() => onControl(name, 'stop')} />}
          {isRunning && <IconAction icon="restart" label={`Restart ${name}`} title="Restart" onClick={() => onControl(name, 'restart')} />}
          {canEnable && (
            <IconAction
              icon="zap"
              label={`Reset circuit and enable ${name}`}
              title="Enable (reset circuit)"
              onClick={() => onControl(name, 'enable')}
            />
          )}
          <IconAction icon="logs" label={`Logs for ${name}`} title="Logs" pressed={showLogs} onClick={() => setShowLogs((v) => !v)} />
          <IconAction icon="edit" label={`Edit ${name}`} title="Edit" onClick={() => onEdit(name)} />
          <IconAction icon="trash" label={`Delete ${name}`} title="Delete" onClick={() => onDelete(name)} />
        </span>
      }
      metric={<Metric label="uptime" value={LIVE_STATES.includes(state) ? formatUptime(uptime) : '—'} />}
      body={
        expanded || showLogs ? (
          <>
            {expanded && (
              <div className="kv">
                <div className="kv-item">
                  <span className="kv-k">Restarts</span>
                  <span className="kv-v mono">{restart_count}</span>
                </div>
                {config.cwd && (
                  <div className="kv-item">
                    <span className="kv-k">CWD</span>
                    <span className="kv-v mono" title={config.cwd}>
                      {config.cwd}
                    </span>
                  </div>
                )}
                {!hideRepo && config.repo && (
                  <div className="kv-item">
                    <span className="kv-k">Repo</span>
                    <span className="kv-v">{config.repo}</span>
                  </div>
                )}
                {config.after.length > 0 && (
                  <div className="kv-item">
                    <span className="kv-k">Depends on</span>
                    <span className="kv-v">{config.after.join(', ')}</span>
                  </div>
                )}
              </div>
            )}
            {showLogs && <LogViewer serviceName={name} />}
          </>
        ) : undefined
      }
    />
  )
}
