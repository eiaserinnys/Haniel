import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api, ApiError } from './api'

/**
 * Endpoint mapping regressions — defends against the E2E review F3/F4
 * findings (CRUD called /api/services/{name} while server only routes
 * /api/config/services/...; reload called /api/config/reload while server
 * exposes /api/reload). Each test asserts the *exact* URL + method + body
 * the request() wrapper sends to fetch().
 *
 * Auth tests mirror orch-server/dashboard/src/lib/api.test.ts patterns:
 * localStorage.clear() in beforeEach so the request() wrapper does not add
 * an Authorization header by default; specific tests opt in by setting
 * localStorage.setItem('haniel-token', ...).
 */
describe('haniel dashboard api client', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
    // Reset window.location.href tracking for 401 redirect tests.
    Object.defineProperty(window, 'location', {
      writable: true,
      value: { href: '' },
    })
  })

  function mockOk<T>(body: T, status = 200) {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(body), { status }),
    )
  }

  // ── endpoint mapping (analysis cache F3/F4) ────────────────────────────────

  it('createService POSTs /api/config/services with {name, config} body', async () => {
    mockOk({ ok: true })
    const cfg = {
      run: 'python app.py',
      cwd: null,
      repo: null,
      after: [],
      ready: null,
      enabled: true,
    }
    await api.createService('web', cfg)
    expect(fetch).toHaveBeenCalledWith('/api/config/services', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'web', config: cfg }),
    })
  })

  it('updateService PUTs /api/config/services/{name} with config body', async () => {
    mockOk({ ok: true })
    const cfg = {
      run: 'python app.py 9090',
      cwd: null,
      repo: null,
      after: [],
      ready: null,
      enabled: true,
    }
    await api.updateService('web', cfg)
    expect(fetch).toHaveBeenCalledWith('/api/config/services/web', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    })
  })

  it('deleteService DELETEs /api/config/services/{name}', async () => {
    mockOk({ ok: true })
    await api.deleteService('web')
    expect(fetch).toHaveBeenCalledWith('/api/config/services/web', {
      method: 'DELETE',
      headers: {},
    })
  })

  it('createRepo POSTs /api/config/repos with {name, config} body', async () => {
    mockOk({ ok: true })
    const cfg = { url: 'git@github.com:x/y.git', branch: 'main', path: './y' }
    await api.createRepo('y', cfg)
    expect(fetch).toHaveBeenCalledWith('/api/config/repos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'y', config: cfg }),
    })
  })

  it('updateRepo PUTs /api/config/repos/{name} with config body', async () => {
    mockOk({ ok: true })
    const cfg = { url: 'git@github.com:x/y.git', branch: 'dev', path: './y' }
    await api.updateRepo('y', cfg)
    expect(fetch).toHaveBeenCalledWith('/api/config/repos/y', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    })
  })

  it('deleteRepo DELETEs /api/config/repos/{name}', async () => {
    mockOk({ ok: true })
    await api.deleteRepo('y')
    expect(fetch).toHaveBeenCalledWith('/api/config/repos/y', {
      method: 'DELETE',
      headers: {},
    })
  })

  it('reload POSTs /api/reload (runtime API, not /api/config/reload)', async () => {
    mockOk({ ok: true, action: 'reload' })
    await api.reload()
    expect(fetch).toHaveBeenCalledWith('/api/reload', {
      method: 'POST',
      headers: {},
    })
  })

  it('getStatus GETs /api/status', async () => {
    mockOk({ services: {}, repos: {} })
    await api.getStatus()
    expect(fetch).toHaveBeenCalledWith('/api/status', { headers: {} })
  })

  it('getConfigRepos GETs /api/config/repos', async () => {
    mockOk({})
    await api.getConfigRepos()
    expect(fetch).toHaveBeenCalledWith('/api/config/repos', { headers: {} })
  })

  // ── Authorization header (analysis cache F5) ───────────────────────────────

  it('omits Authorization when no token is stored', async () => {
    mockOk({ services: {} })
    await api.getStatus()
    expect(fetch).toHaveBeenCalledWith(
      '/api/status',
      expect.objectContaining({ headers: {} }),
    )
  })

  it('attaches Authorization: Bearer when haniel-token is set', async () => {
    localStorage.setItem('haniel-token', 'secret-abc')
    mockOk({ services: {} })
    await api.getStatus()
    expect(fetch).toHaveBeenCalledWith(
      '/api/status',
      expect.objectContaining({
        headers: { Authorization: 'Bearer secret-abc' },
      }),
    )
  })

  it('throws ApiError on non-ok non-401 response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('boom', { status: 500 }),
    )
    await expect(api.getStatus()).rejects.toBeInstanceOf(ApiError)
  })

  it('on 401: clears token, redirects to /auth/login, throws ApiError', async () => {
    localStorage.setItem('haniel-token', 'stale')
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('unauthorized', { status: 401 }),
    )
    await expect(api.getStatus()).rejects.toBeInstanceOf(ApiError)
    expect(localStorage.getItem('haniel-token')).toBeNull()
    expect(window.location.href).toBe('/auth/login')
  })
})
