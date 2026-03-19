// API types matching the haniel backend REST/WS shapes

export type ServiceState =
  | 'running'
  | 'ready'
  | 'starting'
  | 'stopping'
  | 'stopped'
  | 'crashed'
  | 'circuit_open'

export interface ServiceConfig {
  run: string
  cwd: string | null
  repo: string | null
  after: string[]
  ready: string | null
  enabled: boolean
}

export interface ServiceConfigInput {
  run: string
  cwd: string | null
  repo: string | null
  after: string[]
  ready: string | null
  enabled: boolean
}

export interface ServiceStatus {
  state: ServiceState
  uptime: number
  restart_count: number
  consecutive_failures: number
  config: ServiceConfig
}

export interface PendingChanges {
  commits: string[]
  stat: string
}

export interface RepoStatus {
  url: string
  branch: string
  path: string
  last_head: string | null
  pending_changes: PendingChanges | null
  fetch_error: string | null
}

export interface RepoConfigInput {
  url: string
  branch: string
  path: string
}

export interface SelfUpdateStatus {
  repo: string
  pending: boolean
  auto_update: boolean
}

export interface DependencyInfo {
  dependencies: string[]
}

export interface RunnerStatus {
  services: Record<string, ServiceStatus>
  repos: Record<string, RepoStatus>
  self_update: SelfUpdateStatus | null
  dependency_graph: Record<string, DependencyInfo> | null
  poll_count: number
  poll_interval: number
  last_poll: string | null
  pending_restarts: string[]
}

// WebSocket event types

export interface WsInitEvent {
  type: 'init'
  status: RunnerStatus
}

export interface WsStateChangeEvent {
  type: 'state_change'
  service: string
  old: ServiceState
  new: ServiceState
}

export interface WsRepoChangeEvent {
  type: 'repo_change'
  repo: string
  pending_changes: PendingChanges | null
}

export interface WsSelfUpdatePendingEvent {
  type: 'self_update_pending'
  repo: string
}

export interface WsReloadCompleteEvent {
  type: 'reload_complete'
}

export type WsEvent =
  | WsInitEvent
  | WsStateChangeEvent
  | WsRepoChangeEvent
  | WsSelfUpdatePendingEvent
  | WsReloadCompleteEvent
