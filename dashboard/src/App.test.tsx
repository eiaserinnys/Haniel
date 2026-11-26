import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RunnerStatus, WsEvent } from '@/lib/types'

let emit: (event: WsEvent) => void = () => {}

vi.mock('@/hooks/useWebSocket', async () => {
  const { useEffect } = await import('react')
  return {
    useWebSocket: ({ onEvent }: { onEvent: (event: WsEvent) => void }) => {
      useEffect(() => {
        emit = onEvent
      }, [onEvent])
      return { connectionState: 'connected' }
    },
  }
})

import App from './App'

const repo = (head: string, commits: string[] | null) => ({
  url: 'https://github.com/eiaserinnys/x',
  branch: 'main',
  path: './repos/x',
  last_head: head,
  pending_changes: commits ? { commits, stat: '' } : null,
  fetch_error: null,
  pulling: false,
})

const status: RunnerStatus = {
  services: {},
  repos: {
    haniel: repo('1f7e0b85aaaa', ['a1b2c3d fix poll', 'b2c3d4e docs']),
    eb_lore: repo('9d8c7b6eeeee', ['c3d4e5f add index']),
  },
  self_update: { repo: 'haniel', pending: true, auto_update: false, last_result: null },
  dependency_graph: null,
  poll_count: 12,
  poll_interval: 60,
  last_poll: '2026-11-26T12:00:00Z',
  pending_restarts: ['serendipity-api'],
}

let calls: Array<{ path: string; method: string }>

function renderLoaded() {
  render(<App />)
  act(() => emit({ type: 'init', status }))
}

beforeEach(() => {
  localStorage.clear()
  calls = []
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }))
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} })
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    calls.push({ path: String(input), method: init?.method ?? 'GET' })
    return new Response(JSON.stringify(String(input) === '/api/status' ? status : { ok: true, message: 'ok' }), { status: 200 })
  })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const approveCalls = () => calls.filter((c) => c.path === '/api/self-update/approve')

describe('self-update approval confirmation', () => {
  it('opens a confirmation instead of calling the API', () => {
    renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    const dialog = screen.getByRole('alertdialog')
    expect(within(dialog).getByText('Update Haniel now?')).toBeInTheDocument()
    expect(
      within(dialog).getByText("Haniel restarts and this node's services pause briefly. Overlap and adopt services keep running."),
    ).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Approve' })).toBeInTheDocument()
    expect(approveCalls()).toHaveLength(0)
  })

  it('does not call the API when cancelled', async () => {
    renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(approveCalls()).toHaveLength(0)
  })

  it('posts the approval once Approve is pressed in the dialog', async () => {
    renderLoaded()
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Approve' }))

    await waitFor(() => expect(approveCalls()).toEqual([{ path: '/api/self-update/approve', method: 'POST' }]))
    expect(screen.queryByRole('alertdialog', { name: 'Update Haniel now?' })).not.toBeInTheDocument()
  })
})

describe('action names', () => {
  it('names the repo pull button Deploy and keeps calling the pull API', async () => {
    renderLoaded()
    const deploy = screen.getByRole('button', { name: 'Deploy eb_lore' })
    expect(deploy).toHaveTextContent('Deploy')
    expect(screen.queryByRole('button', { name: /^Pull/ })).not.toBeInTheDocument()

    fireEvent.click(deploy)
    await waitFor(() => expect(calls).toContainEqual({ path: '/api/repos/eb_lore/pull', method: 'POST' }))
  })

  it('shows Deploying… on a repo that is being pulled, not Pulling…', () => {
    render(<App />)
    act(() => emit({ type: 'init', status: { ...status, repos: { ...status.repos, eb_lore: { ...status.repos.eb_lore, pulling: true } } } }))

    expect(screen.getByText('Deploying…')).toBeInTheDocument()
    expect(screen.queryByText(/pulling/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Deploy eb_lore' })).not.toBeInTheDocument()
  })

  it('calls the self-update button Approve, not Approve update', () => {
    renderLoaded()
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /approve update/i })).not.toBeInTheDocument()
  })

  it('labels queued crash restarts as Crash restarts queued', () => {
    renderLoaded()
    const label = screen.getAllByText('Crash restarts queued')
    expect(label.length).toBeGreaterThan(0)
    expect(screen.getAllByText('serendipity-api').length).toBeGreaterThan(0)
    expect(screen.queryByText(/restart pending/i)).not.toBeInTheDocument()
  })

  it('keeps the pending metric as the count of commits waiting to deploy', () => {
    renderLoaded()
    const metric = screen.getAllByText('pending').map((el) => el.closest('.r-metric'))
    expect(metric.map((m) => m?.textContent)).toEqual(expect.arrayContaining(['pending2', 'pending1']))
  })
})

describe('reload_complete', () => {
  it('fetches the status again', async () => {
    renderLoaded()
    calls.length = 0
    act(() => emit({ type: 'reload_complete' }))

    await waitFor(() => expect(calls).toContainEqual({ path: '/api/status', method: 'GET' }))
  })
})
