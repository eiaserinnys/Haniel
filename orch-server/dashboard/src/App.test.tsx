import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';

const apiMock = vi.hoisted(() => ({
  fetchPending: vi.fn(),
  fetchNodes: vi.fn(),
  fetchHistory: vi.fn(),
  approveDeploy: vi.fn(),
  rejectDeploy: vi.fn(),
  approveAll: vi.fn(),
  serviceCommand: vi.fn(),
  ApiError: class ApiError extends Error {
    status: number;
    body: string;

    constructor(status: number, body: string) {
      super(`API ${status}: ${body}`);
      this.name = 'ApiError';
      this.status = status;
      this.body = body;
    }
  },
}));

const wsMock = vi.hoisted(() => ({
  status: 'connected',
  handler: undefined as undefined | ((event: unknown) => void),
}));

vi.mock('@/lib/api', () => apiMock);

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => false,
}));

vi.mock('@/hooks/useWebSocket', () => ({
  useWebSocket: (onEvent: (event: unknown) => void) => {
    wsMock.handler = onEvent;
    return { status: wsMock.status, reconnect: vi.fn() };
  },
}));

function resetApiMocks() {
  apiMock.fetchPending.mockResolvedValue({ deploys: [], latest_failure: null });
  apiMock.fetchNodes.mockResolvedValue({ nodes: [] });
  apiMock.fetchHistory.mockResolvedValue({ deploys: [] });
  apiMock.approveDeploy.mockResolvedValue({ deploy_id: 'd1', status: 'deploying' });
  apiMock.rejectDeploy.mockResolvedValue({ deploy_id: 'd1', status: 'rejected' });
  apiMock.approveAll.mockResolvedValue({ approved: [], failed: [] });
  apiMock.serviceCommand.mockResolvedValue({ command_id: 'c1', status: 'sent' });
}

async function waitForInitialSync() {
  await waitFor(() => expect(apiMock.fetchPending).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(apiMock.fetchNodes).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(apiMock.fetchHistory).toHaveBeenCalledTimes(1));
}

function pendingDeploy(id: string) {
  const now = new Date().toISOString();
  return {
    deploy_id: id, node_id: 'n1', repo: id, branch: 'main', status: 'pending',
    commits: ['abc change'], affected_services: [], diff_stat: null,
    detected_at: now, approved_by: null, reject_reason: null, error: null,
    duration_ms: null, created_at: now, updated_at: now,
  };
}

describe('App dashboard sync', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    resetApiMocks();
    wsMock.status = 'connected';
    wsMock.handler = undefined;
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('refreshes deploy lists after terminal deploy websocket events', async () => {
    render(<App />);
    await waitForInitialSync();

    apiMock.fetchPending.mockClear();
    apiMock.fetchHistory.mockClear();

    act(() => {
      wsMock.handler?.({
        type: 'status_change',
        deploy_id: 'd1',
        node_id: 'n1',
        status: 'success',
      });
    });

    await waitFor(() => expect(apiMock.fetchPending).toHaveBeenCalledTimes(1));
    expect(apiMock.fetchHistory).toHaveBeenCalledWith({ includeSuperseded: false });
  });

  it('refreshes all dashboard data after websocket reconnects', async () => {
    wsMock.status = 'reconnecting';
    const { rerender } = render(<App />);
    await waitForInitialSync();

    apiMock.fetchPending.mockClear();
    apiMock.fetchNodes.mockClear();
    apiMock.fetchHistory.mockClear();

    wsMock.status = 'connected';
    rerender(<App />);

    await waitFor(() => expect(apiMock.fetchPending).toHaveBeenCalledTimes(1));
    expect(apiMock.fetchNodes).toHaveBeenCalledTimes(1);
    expect(apiMock.fetchHistory).toHaveBeenCalledWith({ includeSuperseded: false });
  });

  it('refreshes visible tabs on focus as a repair path for missed events', async () => {
    render(<App />);
    await waitForInitialSync();

    apiMock.fetchPending.mockClear();
    apiMock.fetchNodes.mockClear();
    apiMock.fetchHistory.mockClear();

    act(() => {
      window.dispatchEvent(new Event('focus'));
    });

    await waitFor(() => expect(apiMock.fetchPending).toHaveBeenCalledTimes(1));
    expect(apiMock.fetchNodes).toHaveBeenCalledTimes(1);
    expect(apiMock.fetchHistory).toHaveBeenCalledWith({ includeSuperseded: false });
  });

  it('uses pending.latest_failure instead of history to render first-screen failure', async () => {
    apiMock.fetchPending.mockResolvedValue({
      deploys: [],
      latest_failure: {
        deploy_id: 'attempt:failed', node_id: 'n1', repo: 'repo', branch: 'main',
        status: 'failed', commits: [], affected_services: [], diff_stat: null,
        detected_at: new Date().toISOString(), approved_by: null, reject_reason: null,
        error: 'HEAD mismatch', duration_ms: 1, created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    });

    render(<App />);
    await waitForInitialSync();

    expect(screen.getByText('Last deploy failed')).toBeInTheDocument();
    expect(screen.getByText('HEAD mismatch')).toBeInTheDocument();
    expect(screen.getByText('All clear')).toBeInTheDocument();
  });

  it('returns only HTTP-accepted selected ids to the selection owner', async () => {
    apiMock.fetchPending.mockResolvedValue({
      deploys: [pendingDeploy('accepted'), pendingDeploy('rejected')],
      latest_failure: null,
    });
    apiMock.approveDeploy.mockImplementation((id: string) => (
      id === 'accepted'
        ? Promise.resolve({ deploy_id: id, status: 'deploying' })
        : Promise.reject(new Error('HTTP rejected'))
    ));

    render(<App />);
    await waitForInitialSync();
    fireEvent.click(screen.getByText('Select all'));
    fireEvent.click(screen.getByRole('button', { name: /Approve 2 selected/ }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Approve 1 selected/ })).toBeInTheDocument();
    });
    expect(apiMock.approveDeploy).toHaveBeenCalledWith('accepted');
    expect(apiMock.approveDeploy).toHaveBeenCalledWith('rejected');
  });
});
