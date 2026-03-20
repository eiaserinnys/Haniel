import type { RunnerStatus, ServiceConfigInput, RepoConfigInput } from './types'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, options)
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}: ${body}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  getStatus: () => request<RunnerStatus>('/api/status'),

  startService:   (name: string) => request(`/api/services/${name}/start`,   { method: 'POST' }),
  stopService:    (name: string) => request(`/api/services/${name}/stop`,    { method: 'POST' }),
  restartService: (name: string) => request(`/api/services/${name}/restart`, { method: 'POST' }),
  enableService:  (name: string) => request(`/api/services/${name}/enable`,  { method: 'POST' }),

  createService: (name: string, config: ServiceConfigInput) =>
    request(`/api/services/${name}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }),

  updateService: (name: string, config: ServiceConfigInput) =>
    request(`/api/services/${name}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }),

  deleteService: (name: string) => request(`/api/services/${name}`, { method: 'DELETE' }),

  pullRepo: (name: string) => request<{ ok: boolean; repo: string; head: string | null }>(`/api/repos/${name}/pull`, { method: 'POST' }),

  createRepo: (name: string, config: RepoConfigInput) =>
    request(`/api/repos/${name}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }),

  updateRepo: (name: string, config: RepoConfigInput) =>
    request(`/api/repos/${name}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }),

  deleteRepo: (name: string) => request(`/api/repos/${name}`, { method: 'DELETE' }),

  approveSelfUpdate: () => request('/api/self-update/approve', { method: 'POST' }),
  selfRestart: () => request('/api/self/restart', { method: 'POST' }),

  getConfigRepos: () => request<Record<string, RepoConfigInput>>('/api/config/repos'),
  reload: () => request('/api/config/reload', { method: 'POST' }),
}

export function getServiceLogs(name: string, count?: number): Promise<{ lines: string[] }> {
  const params = count ? `?lines=${count}` : ''
  return request<{ lines: string[] }>(`/api/services/${name}/logs${params}`)
}

export function getSelfLogs(_name: string, count?: number): Promise<{ lines: string[] }> {
  const params = count ? `?lines=${count}` : ''
  return request<{ lines: string[] }>(`/api/self/logs${params}`)
}
