// Haniel Orchestrator Dashboard — shared type definitions

export type DeployStatus = 'pending' | 'approved' | 'rejected' | 'deploying' | 'success' | 'failed';

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
}

export interface NodeService {
  name: string;
  port: number | null;
  pid: number | null;
  status: string;
  role: string;
  uptime_ms: number;
  enabled: boolean;
  deps: string[];
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
}

export interface ServiceCommandResultEvent {
  type: 'service_command_result';
  command_id: string;
  node_id: string;
  service_name: string;
  action: string;
  success: boolean;
  error: string | null;
}

export type WsEvent = NewPendingEvent | StatusChangeEvent | NodeConnectedEvent | NodeDisconnectedEvent | ServiceCommandResultEvent;

// API response types — for in-flight tracking and warning surfaces.
// Mirror api.py exactly so that warning/failed are not silently swallowed.

export interface ApproveResponse {
  deploy_id: string;
  status: string; // 'deploying' | 'approved'
  warning?: string; // 'node not connected, will deploy on reconnect'
}

// api.py L142-180 approve_all: always returns approved/failed arrays;
// `message` is added only when there are no pending deploys.
export interface ApproveAllResponse {
  approved: string[];
  failed: Array<{ deploy_id: string; reason: string }>;
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
export type ToastKind = 'info' | 'success' | 'amber' | 'error';

export interface Toast {
  id: string;
  text: string;
  kind: ToastKind;
}
