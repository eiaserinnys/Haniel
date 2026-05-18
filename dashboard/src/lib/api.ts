/**
 * REST API client for the haniel dashboard.
 *
 * Auth pattern matches `orch-server/dashboard/src/lib/api.ts`:
 *   - Bearer token sourced from localStorage key `haniel-token`
 *   - 401 → clear token and redirect to /auth/login
 *
 * Endpoints align with the haniel node server routes:
 *   - CRUD lives under `/api/config/*` (config_api.py)
 *   - Runtime reload is `/api/reload` (api.py)
 *   - Service actions stay under `/api/services/{name}/{action}`
 */
import type { RunnerStatus, ServiceConfigInput, RepoConfigInput } from './types'

const TOKEN_KEY = 'haniel-token'

export class ApiError extends Error {
  status: number
  body: string

  constructor(status: number, body: string) {
    super(`API ${status}: ${body}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token =
    typeof localStorage !== 'undefined'
      ? localStorage.getItem(TOKEN_KEY) || ''
      : ''
  const headers: Record<string, string> = {
    ...((init?.headers as Record<string, string>) || {}),
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  const res = await fetch(path, { ...init, headers })
  if (!res.ok) {
    if (res.status === 401) {
      // Token missing or rejected — clear stale token and redirect to login.
      if (typeof localStorage !== 'undefined') {
        localStorage.removeItem(TOKEN_KEY)
      }
      if (typeof window !== 'undefined') {
        window.location.href = '/auth/login'
      }
      throw new ApiError(res.status, 'Unauthorized')
    }
    const body = await res.text().catch(() => '')
    throw new ApiError(res.status, body)
  }
  return res.json() as Promise<T>
}

export const api = {
  getStatus: () => request<RunnerStatus>('/api/status'),

  startService:   (name: string) => request(`/api/services/${name}/start`,   { method: 'POST' }),
  stopService:    (name: string) => request(`/api/services/${name}/stop`,    { method: 'POST' }),
  restartService: (name: string) => request(`/api/services/${name}/restart`, { method: 'POST' }),
  enableService:  (name: string) => request(`/api/services/${name}/enable`,  { method: 'POST' }),

  // Config CRUD — server routes live under /api/config/*.
  // POST: server expects {name, config} (config_api.py:181-189). Function
  // signature (name, config) is kept; only the body wrapper changes.
  createService: (name: string, config: ServiceConfigInput) =>
    request('/api/config/services', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, config }),
    }),

  updateService: (name: string, config: ServiceConfigInput) =>
    request(`/api/config/services/${name}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }),

  deleteService: (name: string) =>
    request(`/api/config/services/${name}`, { method: 'DELETE' }),

  pullRepo: (name: string) =>
    request<{ ok: boolean; repo: string; head: string | null }>(
      `/api/repos/${name}/pull`,
      { method: 'POST' },
    ),

  createRepo: (name: string, config: RepoConfigInput) =>
    request('/api/config/repos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, config }),
    }),

  updateRepo: (name: string, config: RepoConfigInput) =>
    request(`/api/config/repos/${name}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }),

  deleteRepo: (name: string) =>
    request(`/api/config/repos/${name}`, { method: 'DELETE' }),

  approveSelfUpdate: () => request('/api/self-update/approve', { method: 'POST' }),
  selfRestart: () => request('/api/self/restart', { method: 'POST' }),

  getConfigRepos: () => request<Record<string, RepoConfigInput>>('/api/config/repos'),

  // Runtime reload — single source of truth is /api/reload (api.py:223-233).
  // /api/config/reload does not exist on the server.
  reload: () => request('/api/reload', { method: 'POST' }),
}

export function getServiceLogs(name: string, count?: number): Promise<{ lines: string[] }> {
  const params = count ? `?lines=${count}` : ''
  return request<{ lines: string[] }>(`/api/services/${name}/logs${params}`)
}

export function getSelfLogs(_name: string, count?: number): Promise<{ lines: string[] }> {
  const params = count ? `?lines=${count}` : ''
  return request<{ lines: string[] }>(`/api/self/logs${params}`)
}
