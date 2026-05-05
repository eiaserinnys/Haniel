/**
 * REST API client for orch-server dashboard endpoints.
 * Base URL is same-origin (relative paths).
 */

import type {
  Deploy,
  OrchestratorNode,
  ApproveResponse,
  ApproveAllResponse,
  ServiceCommandResponse,
} from '@/types';

export class ApiError extends Error {
  status: number;
  body: string;

  constructor(status: number, body: string) {
    super(`API ${status}: ${body}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = localStorage.getItem('haniel-token') || '';
  const headers: Record<string, string> = {
    ...(init?.headers as Record<string, string> || {}),
  };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    // If unauthorized, redirect to login
    if (res.status === 401) {
      window.location.href = '/auth/login';
      throw new ApiError(res.status, 'Unauthorized');
    }
    const text = await res.text().catch(() => 'Unknown error');
    throw new ApiError(res.status, text);
  }
  return res.json() as Promise<T>;
}

/* ── Endpoints ─────────────────────────────────────── */

export function fetchPending(): Promise<{ deploys: Deploy[] }> {
  return request('/api/orch/pending');
}

export function fetchNodes(): Promise<{ nodes: OrchestratorNode[] }> {
  return request('/api/orch/nodes');
}

export function fetchHistory(): Promise<{ deploys: Deploy[] }> {
  return request('/api/orch/history');
}

export function approveDeploy(deployId: string): Promise<ApproveResponse> {
  return request('/api/orch/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ deploy_id: deployId }),
  });
}

export function rejectDeploy(deployId: string, reason: string): Promise<{ status: string }> {
  return request('/api/orch/reject', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ deploy_id: deployId, reason }),
  });
}

export function approveAll(): Promise<ApproveAllResponse> {
  return request('/api/orch/approve-all', { method: 'POST' });
}

export function serviceCommand(
  nodeId: string,
  serviceName: string,
  action: 'restart' | 'stop'
): Promise<ServiceCommandResponse> {
  return request('/api/orch/service-command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ node_id: nodeId, service_name: serviceName, action }),
  });
}
