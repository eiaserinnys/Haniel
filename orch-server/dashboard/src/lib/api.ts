/**
 * REST API client for orch-server dashboard endpoints.
 * Base URL is same-origin (relative paths).
 */

import type { Deploy, OrchestratorNode } from '@/types';

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
  const res = await fetch(path, init);
  if (!res.ok) {
    const text = await res.text().catch(() => 'Unknown error');
    throw new ApiError(res.status, text);
  }
  return res.json() as Promise<T>;
}

/* ── Endpoints ───────────────────────────────────���──── */

export function fetchPending(): Promise<{ deploys: Deploy[] }> {
  return request('/api/deploys/pending');
}

export function fetchNodes(): Promise<{ nodes: OrchestratorNode[] }> {
  return request('/api/nodes');
}

export function fetchHistory(): Promise<{ deploys: Deploy[] }> {
  return request('/api/deploys/history');
}

export function approveDeploy(deployId: string): Promise<{ status: string }> {
  return request(`/api/deploys/${encodeURIComponent(deployId)}/approve`, {
    method: 'POST',
  });
}

export function rejectDeploy(deployId: string, reason: string): Promise<{ status: string }> {
  return request(`/api/deploys/${encodeURIComponent(deployId)}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
}

export function approveAll(): Promise<{ approved: string[] }> {
  return request('/api/deploys/approve-all', { method: 'POST' });
}
