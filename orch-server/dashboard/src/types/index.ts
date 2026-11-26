// Haniel Orchestrator Dashboard — shared type definitions

export type DeployStatus = 'pending' | 'rejected' | 'deploying' | 'success' | 'failed';

// Summary of the latest failed attempt for a request (deploy_last_failure.py).
export interface LastFailure {
  kind: string;
  attempt_id: string | null;
  reason: string | null;
  error: string | null;
  at: string | null;
}

// Node-reported deploy stage (deploy_attempts.progress_stage and the
// dashboard WS deploy_progress event).
export type ProgressStage =
  | 'preparing'
  | 'build'
  | 'preflight'
  | 'backing_up'
  | 'migrating'
  | 'starting'
  | 'verifying'
  | 'recovering';

export interface Deploy {
  deploy_id: string;
  node_id: string;
  repo: string;
  branch: string;
  status: DeployStatus;
  commits: string[];  // "hash subject" format from git log --oneline
  affected_services: string[];
  diff_stat: string | null;
  detected_at: string;  // ISO 8601
  approved_by: string | null;
  reject_reason: string | null;
  error: string | null;
  duration_ms: number | null;
  created_at: string;
  updated_at: string;
  is_self_update?: boolean;
  // Pending rows only; absent on History rows.
  inspection_required?: boolean;
  last_failure?: LastFailure | null;
  // 'approval' = the repository's auto-apply is off; null on rows from old nodes.
  apply_policy?: 'auto' | 'approval' | null;
  // Deploying rows only; null until the node reports a stage.
  progress_stage?: ProgressStage | null;
  // History failure rows. failed_stage is a node stage (the PR #114 v2 field);
  // recovery_attempted says the node entered recovery (not that it recovered); null when the node did not say.
  terminal_reason?: string | null;
  failed_stage?: string | null;
  recovery_attempted?: boolean | null;
}

// Service status is split into three axes so the dashboard can show process
// state and readiness independently. See atom 260519.01 — the previous
// single `status` field conflated `running` (HealthState) with the action-
// button trigger and hid edge cases like pid=None + health=ready (race).
//
//   process_status: 'running' iff the node reports a pid for the service,
//                   'stopped' otherwise. Drives action button visibility.
//   health_status:  HealthManager state value
//                   ('ready' | 'running' | 'starting' | 'stopping' |
//                    'crashed' | 'stopped' | 'circuit_open').
//   ready:          process_status === 'running' && (health_status === 'ready' ||
//                   (health_status === 'running' && the service has no ready
//                   condition)).
export interface NodeService {
  name: string;
  port: number | null;
  pid: number | null;
  process_status: 'running' | 'stopped';
  health_status: string;
  ready: boolean;
  role: string;
  uptime_ms: number;
  enabled: boolean;
  deps: string[];
}

export interface RepoFetch {
  repo: string;
  last_fetch: string | null;  // ISO 8601
}

export interface ServingCommit {
  repo: string;
  branch: string;
  target_head: string;
  deployed_at: string;  // ISO 8601
  deploy_id: string;
}

export interface OrchestratorNode {
  node_id: string;
  hostname: string;
  os: string;
  arch: string;
  haniel_version: string;
  connected: number;  // 0 or 1
  last_seen: string;
  created_at: string;
  services?: NodeService[];
  // Seconds. null when the node is not connected or too old to report it.
  poll_interval?: number | null;
  repo_fetches?: RepoFetch[] | null;
  // Last successful deploy per repository; [] when none is recorded.
  serving?: ServingCommit[];
  self_update_deploying?: boolean;
  self_update_deploy_id?: string | null;
}

// WebSocket connection status
export type WsStatus = 'connected' | 'reconnecting' | 'disconnected';

// WebSocket events from orch-server hub.py
export interface NewPendingEvent {
  type: 'new_pending';
  deploy_id: string;
  node_id: string;
  repo: string;
  branch: string;
  detected_at: string;
}

export interface StatusChangeEvent {
  type: 'status_change';
  deploy_id: string;
  status: DeployStatus;
  node_id: string;
  // Present when an entry is rejected (explicit user reject) or auto-superseded
  // by a newer deploy in the same (node, repo, branch). For supersede, the
  // value starts with 'superseded by '. NOTE: there is no separate
  // 'superseded' DeployStatus value — supersede is detected as
  // status === 'rejected' && reject_reason?.startsWith('superseded').
  reject_reason?: string | null;
}

export interface NodeConnectedEvent {
  type: 'node_connected';
  node_id: string;
  hostname: string;
}

export interface NodeDisconnectedEvent {
  type: 'node_disconnected';
  node_id: string;
  reason: string;
  self_update_deploying?: boolean;
  self_update_deploy_id?: string | null;
}

export interface DeployProgressEvent {
  type: 'deploy_progress';
  deploy_id: string;
  node_id: string;
  orchestrator_attempt_id: string;
  stage: string;
}

export interface ServiceCommandResultEvent {
  type: 'service_command_result';
  command_id: string;
  node_id: string;
  service_name: string;
  action: string;
  success: boolean;
  error: string | null;
  result?: unknown;
}

export type WsEvent =
  | NewPendingEvent
  | StatusChangeEvent
  | NodeConnectedEvent
  | NodeDisconnectedEvent
  | DeployProgressEvent
  | ServiceCommandResultEvent;

// API response types — mirror orch-server api.py.

export interface ApproveResponse {
  deploy_id: string;
  status: string; // 'deploying'
}

// approve_all always returns approved/failed arrays; `message` is added only
// when there are no pending deploys, `needs_inspection` only when a request
// whose last deploy failed was skipped.
export interface ApproveAllResponse {
  approved: string[];
  failed: Array<{ deploy_id: string; reason: string }>;
  needs_inspection?: Array<{ deploy_id: string; reason: string }>;
  message?: string; // 'no pending deploys'
}

export interface ServiceCommandResponse {
  command_id: string;
  status: string; // 'sent'
}

// In-flight service command tracking (dashboard-only, dies on WS disconnect).
export interface InFlightCommand {
  commandId: string;
  nodeId: string;
  serviceName: string;
  action: 'restart' | 'stop';
  // Date.now() at insertion. Used by useInFlightCommands.removeWithMinDelay
  // to enforce a minimum spinner display window even when the node responds
  // very fast.
  addedAt: number;
}

// Page navigation
export type Page = 'pending' | 'nodes' | 'history';

// Toast system
export type ToastKind = 'success' | 'busy' | 'warn' | 'neutral' | 'error';

export interface Toast {
  id: string;
  text: string;
  kind: ToastKind;
}

// Per-area load state for the three data lists.
export type LoadState =
  | { status: 'loading' }
  | { status: 'ready' }
  | { status: 'error'; detail: string };
