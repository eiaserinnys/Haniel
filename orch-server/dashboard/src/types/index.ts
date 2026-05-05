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
  status: string;
  role?: string;
  uptime_ms?: number;
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

export type WsEvent = NewPendingEvent | StatusChangeEvent | NodeConnectedEvent | NodeDisconnectedEvent;

// Page navigation
export type Page = 'pending' | 'nodes' | 'history';

// Toast system
export type ToastKind = 'info' | 'success' | 'amber' | 'error';

export interface Toast {
  id: string;
  text: string;
  kind: ToastKind;
}
